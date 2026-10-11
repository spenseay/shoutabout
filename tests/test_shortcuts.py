from __future__ import annotations

import json
import plistlib
import re
from collections import Counter
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from shoutabout import cli, netinfo, shortcuts
from shoutabout.shortcuts import (
    HUBSIGN_URL,
    NO_PUBLIC_MESSAGE,
    NOTHING_HEARD,
    OBJECT_REPLACEMENT,
    SHORTCUT_NAMES,
    TARGET_FILE,
    TEACH_PROMPT,
    TRANSCRIBE_AUDIO_ACTION,
    ShortcutSignError,
    build,
    build_cherri,
    build_dictate,
    build_offline,
    build_target_toggle,
    build_teach,
    sign_with_hubsign,
    to_plist_bytes,
    transcribe_url,
    vocab_url,
)

SSID = "ExampleHomeWiFi"
LAN = "http://10.0.0.5:8766/transcribe"
PUBLIC = "https://dictate.example.test/transcribe"
LAN_VOCAB = "http://10.0.0.5:8766/vocab"
PUBLIC_VOCAB = "https://dictate.example.test/vocab"
PREFIX = "is.workflow.actions."
ALL_FILES = sorted(f"{name}.plist" for name in SHORTCUT_NAMES.values())


@pytest.fixture
def dictate() -> dict[str, Any]:
    return build_dictate(SSID, LAN, PUBLIC)


@pytest.fixture
def dictate_lan_only() -> dict[str, Any]:
    return build_dictate(SSID, LAN, None)


@pytest.fixture
def teach() -> dict[str, Any]:
    return build_teach(SSID, LAN, PUBLIC)


@pytest.fixture
def teach_lan_only() -> dict[str, Any]:
    return build_teach(SSID, LAN, None)


ALL_BUILDS = {
    "dictate": lambda: build_dictate(SSID, LAN, PUBLIC),
    "dictate-lan-only": lambda: build_dictate(SSID, LAN, None),
    "target-toggle": build_target_toggle,
    "teach": lambda: build_teach(SSID, LAN, PUBLIC),
    "teach-lan-only": lambda: build_teach(SSID, LAN, None),
    "offline": build_offline,
}


# ------------------------------------------------------------------------ helpers


def actions(shortcut: dict[str, Any]) -> list[dict[str, Any]]:
    return shortcut["WFWorkflowActions"]


def ident(action: dict[str, Any]) -> str:
    return action["WFWorkflowActionIdentifier"].removeprefix(PREFIX)


def params(action: dict[str, Any]) -> dict[str, Any]:
    return action["WFWorkflowActionParameters"]


def of_kind(shortcut: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [a for a in actions(shortcut) if ident(a) == kind]


def uuid_of(shortcut: dict[str, Any], kind: str) -> str:
    (action,) = of_kind(shortcut, kind)
    return params(action)["UUID"]


def walk(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def text_of(field: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    assert field["WFSerializationType"] == "WFTextTokenString"
    return field["Value"]["string"], field["Value"]["attachmentsByRange"]


def by_uuid(shortcut: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {params(a)["UUID"]: a for a in actions(shortcut) if "UUID" in params(a)}


# ----------------------------------------------------------------- plist basics


@pytest.mark.parametrize("build", ALL_BUILDS.values(), ids=ALL_BUILDS.keys())
def test_plist_round_trips(build):
    shortcut = build()
    xml = to_plist_bytes(shortcut)
    assert xml.startswith(b"<?xml")
    assert plistlib.loads(xml) == shortcut


@pytest.mark.parametrize("build", ALL_BUILDS.values(), ids=ALL_BUILDS.keys())
def test_every_output_reference_exists(build):
    shortcut = build()
    defined = set(by_uuid(shortcut))
    referenced = {node["OutputUUID"] for node in walk(shortcut) if "OutputUUID" in node}
    assert referenced
    assert referenced <= defined


@pytest.mark.parametrize("build", ALL_BUILDS.values(), ids=ALL_BUILDS.keys())
def test_conditionals_are_well_formed(build):
    shortcut = build()
    modes: dict[str, list[int]] = {}
    for action in of_kind(shortcut, "conditional"):
        p = params(action)
        modes.setdefault(p["GroupingIdentifier"], []).append(p["WFControlFlowMode"])
    assert modes
    for group, seen in modes.items():
        counts = Counter(seen)
        assert counts[0] == 1, group
        assert counts[2] == 1, group
        assert counts[1] <= 1, group
        assert seen == sorted(seen), group  # If, then Otherwise, then End If


@pytest.mark.parametrize("build", ALL_BUILDS.values(), ids=ALL_BUILDS.keys())
def test_notification_bodies_always_have_literal_text(build):
    notifications = of_kind(build(), "notification")
    assert notifications
    for action in notifications:
        body, _ = text_of(params(action)["WFNotificationActionBody"])
        assert body.replace(OBJECT_REPLACEMENT, "").strip()
        assert params(action)["WFNotificationActionTitle"]


# ----------------------------------------------------------------------- Dictate


def test_dictate_has_three_import_questions_for_the_secrets(dictate):
    questions = dictate["WFWorkflowImportQuestions"]
    assert len(questions) == 3
    assert sorted(q["ActionIndex"] for q in questions) == [0, 1, 2]
    for question in questions:
        assert question["Category"] == "Parameter"
        assert question["ParameterKey"] == "WFTextActionText"
        assert question["DefaultValue"] == ""
        assert "QR code" not in question["Text"]
        target = actions(dictate)[question["ActionIndex"]]
        assert ident(target) == "gettext"
        assert params(target)["WFTextActionText"] == ""
    assert "hub token" in questions[0]["Text"].lower()
    assert "token --qr" in questions[0]["Text"]


def test_dictate_uploads_the_recording_as_the_body(dictate):
    audio = uuid_of(dictate, "recordaudio")
    requests = of_kind(dictate, "downloadurl")
    assert len(requests) == 2
    for request in requests:
        p = params(request)
        assert p["WFHTTPMethod"] == "POST"
        assert p["WFHTTPBodyType"] == "File"
        assert "WFFormValues" not in p
        assert p["WFRequestVariable"]["WFSerializationType"] == "WFTextTokenAttachment"
        assert p["WFRequestVariable"]["Value"]["OutputUUID"] == audio


def test_dictate_urls_carry_the_target(dictate):
    # The first Get Text from Input reads dictation-target.txt (the second converts the status).
    target_text = of_kind(dictate, "detect.text")[0]
    target_uuid = params(target_text)["UUID"]
    (opened,) = of_kind(dictate, "documentpicker.open")
    assert params(opened)["WFGetFilePath"] == TARGET_FILE
    assert params(opened)["WFFileErrorIfNotFound"] is False
    assert params(target_text)["WFInput"]["Value"]["OutputUUID"] == params(opened)["UUID"]

    lan_request, public_request = of_kind(dictate, "downloadurl")
    for request, base in ((lan_request, LAN), (public_request, PUBLIC)):
        url, attachments = text_of(params(request)["WFURL"])
        assert url == f"{base}?target={OBJECT_REPLACEMENT}"
        assert list(attachments) == [f"{{{len(base) + len('?target=')}, 1}}"]
        assert next(iter(attachments.values()))["OutputUUID"] == target_uuid


def test_dictate_headers(dictate):
    token = params(actions(dictate)[0])["UUID"]
    lan_request, public_request = of_kind(dictate, "downloadurl")

    def headers(request):
        items = params(request)["WFHTTPHeaders"]["Value"]["WFDictionaryFieldValueItems"]
        return {text_of(i["WFKey"])[0]: text_of(i["WFValue"]) for i in items}

    lan = headers(lan_request)
    assert list(lan) == ["Authorization"]
    public = headers(public_request)
    assert list(public) == ["CF-Access-Client-Id", "CF-Access-Client-Secret", "Authorization"]
    for value, attachments in (lan["Authorization"], public["Authorization"]):
        assert value == "Bearer " + OBJECT_REPLACEMENT
        assert attachments["{7, 1}"]["OutputUUID"] == token


def test_dictate_routes_on_the_wifi_name(dictate):
    wifi = uuid_of(dictate, "getwifi")
    route = [
        a
        for a in of_kind(dictate, "conditional")
        if params(a)["WFControlFlowMode"] == 0
        and "WFConditions" in params(a)
        and params(a)["WFConditions"]["Value"]["WFActionParameterFilterTemplates"][0][
            "WFConditionalActionString"
        ]
        == SSID
    ]
    assert len(route) == 1
    template = params(route[0])["WFConditions"]["Value"]["WFActionParameterFilterTemplates"][0]
    assert template["WFCondition"] == 4  # "is"
    assert template["WFInput"]["Variable"]["Value"]["OutputUUID"] == wifi


def test_dictate_compares_status_as_text_and_copies_the_transcript(dictate):
    lookup = by_uuid(dictate)
    keys = {params(a)["WFDictionaryKey"]: a for a in of_kind(dictate, "getvalueforkey")}
    assert set(keys) == {"text", "warning", "status", "detail"}

    (clipboard,) = of_kind(dictate, "setclipboard")
    assert params(clipboard)["WFInput"]["Value"]["OutputUUID"] == params(keys["text"])["UUID"]

    (status_if,) = [
        a
        for a in of_kind(dictate, "conditional")
        if params(a)["WFControlFlowMode"] == 0
        and params(a)["WFConditions"]["Value"]["WFActionParameterFilterTemplates"][0][
            "WFConditionalActionString"
        ]
        == "ok"
    ]
    template = params(status_if)["WFConditions"]["Value"]["WFActionParameterFilterTemplates"][0]
    converted = lookup[template["WFInput"]["Variable"]["Value"]["OutputUUID"]]
    assert ident(converted) == "detect.text"  # Get Text from Input, not the raw value
    assert params(converted)["WFInput"]["Value"]["OutputUUID"] == params(keys["status"])["UUID"]

    # The notification lives only in the Otherwise branch of that If.
    group = params(status_if)["GroupingIdentifier"]
    sequence = actions(dictate)
    start = next(
        i
        for i, a in enumerate(sequence)
        if ident(a) == "conditional"
        and params(a)["GroupingIdentifier"] == group
        and params(a)["WFControlFlowMode"] == 1
    )
    assert ident(sequence[start - 1]) == "conditional"  # nothing in the "ok" branch
    assert ident(sequence[start + 1]) == "notification"
    body, attachments = text_of(params(sequence[start + 1])["WFNotificationActionBody"])
    assert body.startswith("Check the PC. ")
    referenced = {a["OutputUUID"] for a in attachments.values()}
    assert referenced == {params(keys["warning"])["UUID"], params(keys["detail"])["UUID"]}


def test_dictate_lan_only_variant(dictate_lan_only):
    requests = of_kind(dictate_lan_only, "downloadurl")
    assert len(requests) == 1
    url, _ = text_of(params(requests[0])["WFURL"])
    assert url.startswith(LAN + "?target=")

    questions = dictate_lan_only["WFWorkflowImportQuestions"]
    assert [q["ActionIndex"] for q in questions] == [0]
    assert ident(actions(dictate_lan_only)[0]) == "gettext"
    assert len(of_kind(dictate_lan_only, "gettext")) == 1

    sequence = actions(dictate_lan_only)
    kinds = [ident(a) for a in sequence]
    i = kinds.index("notification")
    body, _ = text_of(params(sequence[i])["WFNotificationActionBody"])
    assert body == NO_PUBLIC_MESSAGE
    assert kinds[i + 1] == "exit"
    assert b"CF-Access" not in to_plist_bytes(dictate_lan_only)


@pytest.mark.parametrize("build", ALL_BUILDS.values(), ids=ALL_BUILDS.keys())
def test_no_secrets_in_the_plist(build):
    xml = to_plist_bytes(build())
    text = xml.decode("utf-8")
    assert "cfast_" not in text
    # "Bearer " is only ever followed by the hub-token variable, never a literal token.
    assert re.search(r"Bearer [^\ufffc<]", text) is None
    for action in of_kind(build(), "gettext"):
        assert params(action)["WFTextActionText"] in ("", "pc", "phone")


def test_dictate_contains_the_given_values_only(dictate):
    text = to_plist_bytes(dictate).decode("utf-8")
    assert SSID in text
    assert LAN + "?target=" in text
    assert PUBLIC + "?target=" in text


@pytest.mark.parametrize(
    ("ssid", "lan", "public"),
    [
        ("", LAN, PUBLIC),
        ("   ", LAN, PUBLIC),
        (SSID, "10.0.0.5:8766/transcribe", PUBLIC),
        (SSID, "ftp://10.0.0.5/transcribe", PUBLIC),
        (SSID, LAN + "?target=pc", PUBLIC),
        (SSID, LAN, "http://dictate.example.test/transcribe"),
        (SSID, LAN, ""),
    ],
)
def test_build_dictate_rejects_bad_input(ssid, lan, public):
    with pytest.raises(ValueError):
        build_dictate(ssid, lan, public)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://10.0.0.5:8766", "http://10.0.0.5:8766/transcribe"),
        ("http://10.0.0.5:8766/", "http://10.0.0.5:8766/transcribe"),
        (" http://10.0.0.5:8766/transcribe/ ", "http://10.0.0.5:8766/transcribe"),
        ("HTTPS://dictate.example.test/transcribe", "https://dictate.example.test/transcribe"),
        ("http://[fd00::5]:8766", "http://[fd00::5]:8766/transcribe"),
    ],
)
def test_transcribe_url(url, expected):
    assert transcribe_url(url) == expected


# ------------------------------------------------------------- Dictation Target


def test_target_toggle():
    shortcut = build_target_toggle()
    assert shortcut["WFWorkflowImportQuestions"] == []
    (save,) = of_kind(shortcut, "documentpicker.save")
    p = params(save)
    assert p["WFFileDestinationPath"] == TARGET_FILE
    assert p["WFSaveFileOverwrite"] is True
    assert p["WFAskWhereToSave"] is False
    assert sorted(params(a)["WFTextActionText"] for a in of_kind(shortcut, "gettext")) == [
        "pc",
        "phone",
    ]
    (opened,) = of_kind(shortcut, "documentpicker.open")
    assert params(opened)["WFFileErrorIfNotFound"] is False


# ------------------------------------------------------------- Teach ShoutAbout


def index_refs(shortcut: dict[str, Any], value: Any) -> Any:
    """``value`` with every OutputUUID replaced by the index of the action that makes it."""
    index = {params(a)["UUID"]: i for i, a in enumerate(actions(shortcut)) if "UUID" in params(a)}
    if isinstance(value, dict):
        return {
            k: (f"action#{index[v]}" if k == "OutputUUID" else index_refs(shortcut, v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [index_refs(shortcut, item) for item in value]
    return value


def headers_of(shortcut: dict[str, Any]) -> list[Any]:
    return [
        index_refs(shortcut, params(request)["WFHTTPHeaders"])
        for request in of_kind(shortcut, "downloadurl")
    ]


def route_condition(shortcut: dict[str, Any]) -> dict[str, Any]:
    wifi = uuid_of(shortcut, "getwifi")
    (route,) = [
        a
        for a in of_kind(shortcut, "conditional")
        if params(a)["WFControlFlowMode"] == 0
        and params(a)["WFConditions"]["Value"]["WFActionParameterFilterTemplates"][0]["WFInput"][
            "Variable"
        ]["Value"]["OutputUUID"]
        == wifi
    ]
    return params(route)["WFConditions"]["Value"]["WFActionParameterFilterTemplates"][0]


@pytest.mark.parametrize("public", [PUBLIC, None], ids=["public", "lan-only"])
def test_teach_asks_the_same_import_questions_as_dictate(public):
    dictate, teach = build_dictate(SSID, LAN, public), build_teach(SSID, LAN, public)
    assert teach["WFWorkflowImportQuestions"] == dictate["WFWorkflowImportQuestions"]
    count = len(dictate["WFWorkflowImportQuestions"])
    for i in range(count):  # the same empty, named Text actions at the same indexes
        a, b = params(actions(dictate)[i]), params(actions(teach)[i])
        assert ident(actions(teach)[i]) == "gettext"
        assert (b["WFTextActionText"], b["CustomOutputName"]) == (
            a["WFTextActionText"],
            a["CustomOutputName"],
        )
    assert ident(actions(teach)[count]) == "ask"


@pytest.mark.parametrize("public", [PUBLIC, None], ids=["public", "lan-only"])
def test_teach_routes_and_authenticates_like_dictate(public):
    dictate, teach = build_dictate(SSID, LAN, public), build_teach(SSID, LAN, public)
    assert headers_of(teach) == headers_of(dictate)
    assert params(of_kind(teach, "getwifi")[0]) | {"UUID": 0} == params(
        of_kind(dictate, "getwifi")[0]
    ) | {"UUID": 0}
    t, d = route_condition(teach), route_condition(dictate)
    assert (t["WFCondition"], t["WFConditionalActionString"]) == (4, SSID)
    assert (d["WFCondition"], d["WFConditionalActionString"]) == (4, SSID)
    assert len(of_kind(teach, "exit")) == len(of_kind(dictate, "exit"))


def test_teach_posts_the_term_as_json(teach):
    ask = uuid_of(teach, "ask")
    lan_request, public_request = of_kind(teach, "downloadurl")
    for request, url in ((lan_request, LAN_VOCAB), (public_request, PUBLIC_VOCAB)):
        p = params(request)
        assert text_of(p["WFURL"]) == (url, {})
        assert p["WFHTTPMethod"] == "POST"
        assert p["WFHTTPBodyType"] == "JSON"
        assert "WFRequestVariable" not in p
        assert "WFFormValues" not in p
        assert p["WFJSONValues"]["WFSerializationType"] == "WFDictionaryFieldValue"
        (item,) = p["WFJSONValues"]["Value"]["WFDictionaryFieldValueItems"]
        assert item["WFItemType"] == 0  # Text
        assert text_of(item["WFKey"]) == ("term", {})
        value, attachments = text_of(item["WFValue"])
        assert value == OBJECT_REPLACEMENT
        assert attachments["{0, 1}"]["OutputUUID"] == ask


def test_teach_asks_for_one_line_of_text(teach):
    (ask,) = of_kind(teach, "ask")
    p = params(ask)
    assert p["WFAskActionPrompt"] == TEACH_PROMPT
    assert p["WFInputType"] == "Text"
    assert p["WFAllowsMultilineText"] is False


def test_teach_shows_exactly_one_notification_per_status(teach):
    lookup = by_uuid(teach)
    keys = {
        params(a)["WFDictionaryKey"]: params(a)["UUID"] for a in of_kind(teach, "getvalueforkey")
    }
    assert set(keys) == {"status", "term", "detail"}
    opening = [a for a in of_kind(teach, "conditional") if params(a)["WFControlFlowMode"] == 0]
    status_ifs = {}
    for action in opening:
        template = params(action)["WFConditions"]["Value"]["WFActionParameterFilterTemplates"][0]
        source = lookup[template["WFInput"]["Variable"]["Value"]["OutputUUID"]]
        if ident(source) == "detect.text":  # the status, converted to Text first
            assert template["WFCondition"] == 4
            assert params(source)["WFInput"]["Value"]["OutputUUID"] == keys["status"]
            status_ifs[template["WFConditionalActionString"]] = params(action)
    assert set(status_ifs) == {"added", "exists"}

    # If added: Taught / Otherwise: (If exists: Already / Otherwise: Not added) — nested.
    sequence = [ident(a) for a in actions(teach)]
    tail = sequence[sequence.index("detect.text") :]
    assert tail == [
        "detect.text",
        "conditional",  # If status is added
        "notification",
        "conditional",  # Otherwise
        "conditional",  # If status is exists
        "notification",
        "conditional",  # Otherwise
        "notification",
        "conditional",  # End If (exists)
        "conditional",  # End If (added)
    ]
    bodies = []
    for action in of_kind(teach, "notification")[-3:]:
        body, attachments = text_of(params(action)["WFNotificationActionBody"])
        bodies.append((body, {a["OutputUUID"] for a in attachments.values()}))
    assert bodies == [
        ("Taught: " + OBJECT_REPLACEMENT, {keys["term"]}),
        ("Already in your vocab: " + OBJECT_REPLACEMENT, {keys["term"]}),
        ("Not added: " + OBJECT_REPLACEMENT, {keys["detail"]}),
    ]


def test_teach_lan_only_variant(teach_lan_only):
    (request,) = of_kind(teach_lan_only, "downloadurl")
    assert text_of(params(request)["WFURL"])[0] == LAN_VOCAB
    assert [q["ActionIndex"] for q in teach_lan_only["WFWorkflowImportQuestions"]] == [0]
    kinds = [ident(a) for a in actions(teach_lan_only)]
    i = kinds.index("notification")
    assert text_of(params(actions(teach_lan_only)[i])["WFNotificationActionBody"])[0] == (
        NO_PUBLIC_MESSAGE
    )
    assert kinds[i + 1] == "exit"
    assert b"CF-Access" not in to_plist_bytes(teach_lan_only)


@pytest.mark.parametrize(
    ("ssid", "lan", "public"),
    [
        ("", LAN, PUBLIC),
        (SSID, "10.0.0.5:8766", PUBLIC),
        (SSID, LAN + "?term=x", PUBLIC),
        (SSID, LAN, "http://dictate.example.test"),
    ],
)
def test_build_teach_rejects_bad_input(ssid, lan, public):
    with pytest.raises(ValueError):
        build_teach(ssid, lan, public)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://10.0.0.5:8766", "http://10.0.0.5:8766/vocab"),
        ("http://10.0.0.5:8766/", "http://10.0.0.5:8766/vocab"),
        ("http://10.0.0.5:8766/transcribe", "http://10.0.0.5:8766/vocab"),
        ("http://10.0.0.5:8766/vocab/", "http://10.0.0.5:8766/vocab"),
        ("HTTPS://dictate.example.test/hub/transcribe", "https://dictate.example.test/hub/vocab"),
    ],
)
def test_vocab_url(url, expected):
    assert vocab_url(url) == expected


# ------------------------------------------------------------ Dictate (offline)


def test_offline_records_and_transcribes_on_the_phone():
    shortcut = build_offline()
    assert shortcut["WFWorkflowImportQuestions"] == []
    assert [ident(a) for a in actions(shortcut)] == [
        "recordaudio",
        TRANSCRIBE_AUDIO_ACTION,  # an App Intent identifier, not is.workflow.actions.*
        "count",
        "conditional",
        "notification",
        "conditional",
        "setclipboard",
        "conditional",
    ]
    record, transcribe, count, if_empty, notify, _, copy, _ = actions(shortcut)
    assert params(record)["WFRecordingCompression"] == "Normal"
    assert params(record)["WFRecordingStart"] == "Immediately"
    assert params(transcribe)["audio"]["Value"]["OutputUUID"] == params(record)["UUID"]
    assert params(count)["WFCountType"] == "Characters"
    assert params(count)["Input"]["Value"]["OutputUUID"] == params(transcribe)["UUID"]
    template = params(if_empty)["WFConditions"]["Value"]["WFActionParameterFilterTemplates"][0]
    assert template["WFCondition"] == 4
    assert template["WFNumberValue"] == 0
    assert "WFConditionalActionString" not in template
    assert template["WFInput"]["Variable"]["Value"]["OutputUUID"] == params(count)["UUID"]
    assert text_of(params(notify)["WFNotificationActionBody"]) == (NOTHING_HEARD, {})
    # Copied only when something was heard, so an empty result doesn't wipe the clipboard.
    assert params(copy)["WFInput"]["Value"]["OutputUUID"] == params(transcribe)["UUID"]


def test_offline_needs_no_network_and_no_secrets():
    text = repr(actions(build_offline()))  # not the plist's DOCTYPE, which has an http:// URL
    for needle in ("downloadurl", "getwifi", "gettext", "http://", "https://", "Bearer"):
        assert needle not in text


def test_build_by_key():
    assert set(SHORTCUT_NAMES) == set(cli.SHORTCUT_KEYS)
    assert tuple(SHORTCUT_NAMES) == cli.SHORTCUT_KEYS  # same output order
    for key in SHORTCUT_NAMES:
        shortcut = build(key, SSID, LAN, PUBLIC)
        assert plistlib.loads(to_plist_bytes(shortcut)) == shortcut
    assert of_kind(build("offline", None, None, None), "recordaudio")
    with pytest.raises(ValueError, match="Wi-Fi"):
        build("teach", None, None, None)
    with pytest.raises(ValueError, match="unknown"):
        build("nope", SSID, LAN, PUBLIC)


# ----------------------------------------------------------------------- signing


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_sign_with_hubsign_success():
    xml = to_plist_bytes(build_target_toggle())
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["body"] = json.loads(request.content)
        seen["agent"] = request.headers["user-agent"]
        return httpx.Response(200, content=b"AEA1" + b"\x00signed")

    with _client(handler) as client:
        signed = sign_with_hubsign("Dictation Target", xml, client)
    assert signed == b"AEA1\x00signed"
    assert seen["url"] == HUBSIGN_URL
    assert seen["method"] == "POST"
    assert seen["body"] == {"shortcutName": "Dictation Target", "shortcut": xml.decode()}
    assert seen["agent"].startswith("shoutabout/")


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(500, text="upstream exploded"), "HTTP 500: upstream exploded"),
        (httpx.Response(429, text=""), "HTTP 429"),
        (
            httpx.Response(302, headers={"location": "https://www.example.test/"}),
            "redirecting to https://www.example.test/",
        ),
        (httpx.Response(200, content=b"<html>not signed</html>"), "isn't a signed shortcut"),
    ],
)
def test_sign_with_hubsign_rejects_bad_replies(response, message):
    with _client(lambda request: response) as client, pytest.raises(ShortcutSignError) as err:
        sign_with_hubsign("Dictate", b"<plist/>", client)
    assert message in str(err.value)


def test_sign_with_hubsign_network_error():
    def handler(request):
        raise httpx.ConnectError("no route to host", request=request)

    with _client(handler) as client, pytest.raises(ShortcutSignError, match="couldn't reach"):
        sign_with_hubsign("Dictate", b"<plist/>", client)


# ------------------------------------------------------------- CLI make-shortcuts


def _load(path) -> dict[str, Any]:
    return plistlib.loads(path.read_bytes())


def _urls(shortcut: dict[str, Any]) -> list[str]:
    return [text_of(params(a)["WFURL"])[0] for a in of_kind(shortcut, "downloadurl")]


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("make-shortcuts must not sign in this test")

    monkeypatch.setattr(shortcuts, "sign_with_hubsign", refuse)
    monkeypatch.setattr(netinfo, "wifi_ssid", lambda: None)


def test_cli_no_sign_writes_all_four_plists(home, tmp_path, capsys, no_network):
    out = tmp_path / "out"
    rc = cli.main(
        ["make-shortcuts", "--no-sign", "--ssid", "X", "--lan-url", LAN, "--no-public"]
        + ["--out", str(out)]
    )
    assert rc == 0
    assert sorted(p.name for p in out.iterdir()) == ALL_FILES
    dictate = _load(out / "Dictate.plist")
    assert _urls(dictate) == [f"{LAN}?target={OBJECT_REPLACEMENT}"]
    assert len(dictate["WFWorkflowImportQuestions"]) == 1
    assert _load(out / "Dictation Target.plist")["WFWorkflowImportQuestions"] == []
    teach = _load(out / "Teach ShoutAbout.plist")
    assert _urls(teach) == [LAN_VOCAB]
    assert len(teach["WFWorkflowImportQuestions"]) == 1
    offline = _load(out / "Dictate (offline).plist")
    assert offline["WFWorkflowImportQuestions"] == []
    assert not of_kind(offline, "downloadurl")
    printed = capsys.readouterr().out
    assert "HubSign" not in printed
    assert str(out.resolve()) in printed


def test_cli_defaults_from_config_and_install_state(home, tmp_path, monkeypatch, no_network):
    monkeypatch.setattr(netinfo, "lan_ipv4", lambda: "192.168.1.50")
    home.mkdir(parents=True)
    (home / "install-state.json").write_text(
        json.dumps({"public_hostname": "dictate.example.test"}), "utf-8"
    )
    out = tmp_path / "out"
    assert cli.main(["make-shortcuts", "--no-sign", "--ssid", "X", "--out", str(out)]) == 0
    dictate = _load(out / "Dictate.plist")
    assert _urls(dictate) == [
        f"http://192.168.1.50:47821/transcribe?target={OBJECT_REPLACEMENT}",
        f"https://dictate.example.test/transcribe?target={OBJECT_REPLACEMENT}",
    ]
    assert len(dictate["WFWorkflowImportQuestions"]) == 3


def test_cli_without_public_hostname_is_lan_only(home, tmp_path, capsys, no_network):
    out = tmp_path / "out"
    rc = cli.main(
        ["make-shortcuts", "--no-sign", "--ssid", "X", "--lan-url", LAN] + ["--out", str(out)]
    )
    assert rc == 0
    assert len(of_kind(_load(out / "Dictate.plist"), "downloadurl")) == 1
    assert "no public_hostname" in capsys.readouterr().out


def test_cli_detects_the_ssid(home, tmp_path, capsys, monkeypatch, no_network):
    monkeypatch.setattr(netinfo, "wifi_ssid", lambda: "Detected Net")
    out = tmp_path / "out"
    rc = cli.main(["make-shortcuts", "--no-sign", "--lan-url", LAN, "--out", str(out)])
    assert rc == 0
    assert "'Detected Net'" in capsys.readouterr().out
    assert "Detected Net" in (out / "Dictate.plist").read_text("utf-8")


def test_cli_needs_an_ssid(home, tmp_path, capsys, no_network):
    out = tmp_path / "out"
    assert cli.main(["make-shortcuts", "--no-sign", "--lan-url", LAN, "--out", str(out)]) == 1
    assert "--ssid" in capsys.readouterr().err
    assert not out.exists()


def test_cli_rejects_a_bad_url(home, tmp_path, capsys, no_network):
    out = tmp_path / "out"
    rc = cli.main(
        ["make-shortcuts", "--no-sign", "--ssid", "X", "--lan-url", LAN]
        + ["--public-url", "http://dictate.example.test", "--out", str(out)]
    )
    assert rc == 1
    assert "https://" in capsys.readouterr().err
    assert not out.exists()


def test_cli_signs_by_default(home, tmp_path, capsys, monkeypatch):
    signed = []

    def fake_sign(name, xml, client=None):
        plistlib.loads(xml)
        signed.append(name)
        return b"AEA1" + name.encode()

    monkeypatch.setattr(shortcuts, "sign_with_hubsign", fake_sign)
    out = tmp_path / "out"
    rc = cli.main(
        ["make-shortcuts", "--ssid", "X", "--lan-url", LAN, "--no-public", "--out", str(out)]
    )
    assert rc == 0
    assert signed == ["Dictate", "Dictation Target", "Teach ShoutAbout", "Dictate (offline)"]
    assert (out / "Dictate.shortcut").read_bytes() == b"AEA1Dictate"
    assert (out / "Dictation Target.shortcut").read_bytes() == b"AEA1Dictation Target"
    assert (out / "Dictate (offline).shortcut").read_bytes() == b"AEA1Dictate (offline)"
    printed = capsys.readouterr().out
    assert "HubSign" in printed
    assert "--no-sign" in printed
    assert "Dictate and Teach ShoutAbout" in printed  # who asks import questions


def test_cli_sign_failure_exits_1(home, tmp_path, capsys, monkeypatch):
    def fail(name, xml, client=None):
        raise ShortcutSignError("HubSign answered HTTP 502")

    monkeypatch.setattr(shortcuts, "sign_with_hubsign", fail)
    out = tmp_path / "out"
    rc = cli.main(
        ["make-shortcuts", "--ssid", "X", "--lan-url", LAN, "--no-public", "--out", str(out)]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "HTTP 502" in err
    assert "--no-sign" in err
    assert not out.exists()


def test_cli_which_filters(home, tmp_path, capsys, no_network):
    out = tmp_path / "out"
    rc = cli.main(
        ["make-shortcuts", "--no-sign", "--ssid", "X", "--lan-url", LAN, "--no-public"]
        + ["--which", "target,dictate", "--out", str(out)]
    )
    assert rc == 0
    assert sorted(p.name for p in out.iterdir()) == ["Dictate.plist", "Dictation Target.plist"]
    assert "Teach" not in capsys.readouterr().out


def test_cli_offline_and_target_need_no_wifi_name(home, tmp_path, capsys, no_network):
    out = tmp_path / "out"
    rc = cli.main(["make-shortcuts", "--no-sign", "--which", "offline,target", "--out", str(out)])
    assert rc == 0  # wifi_ssid() returns None here, and no --ssid was given
    assert sorted(p.name for p in out.iterdir()) == [
        "Dictate (offline).plist",
        "Dictation Target.plist",
    ]
    printed = capsys.readouterr().out
    assert "Home Wi-Fi" not in printed
    assert "LAN URL" not in printed


def test_cli_teach_alone_needs_the_wifi_name(home, tmp_path, capsys, no_network):
    out = tmp_path / "out"
    rc = cli.main(
        ["make-shortcuts", "--no-sign", "--which", "teach", "--lan-url", LAN, "--out", str(out)]
    )
    assert rc == 1
    assert "--ssid" in capsys.readouterr().err
    assert not out.exists()


@pytest.mark.parametrize("value", ["nope", "dictate,nope", ",", ""])
def test_cli_which_rejects_unknown_names(home, tmp_path, capsys, no_network, value):
    out = tmp_path / "out"
    assert cli.main(["make-shortcuts", "--which", value, "--out", str(out)]) == 2
    assert "dictate,target,teach,offline" in capsys.readouterr().err
    assert not out.exists()


def test_parse_which_keeps_the_canonical_order():
    assert cli.parse_which(" Offline, DICTATE ,offline") == ["dictate", "offline"]
    assert cli.parse_which("dictate,target,teach,offline") == list(cli.SHORTCUT_KEYS)


# ------------------------------------------------------------------ Cherri source
#
# No Cherri compiler is available here (the output was checked by hand with the Cherri 2.3.0
# playground), so these check the shape of the source (balanced brackets and strings, every
# name defined before use, questions bound once) and that it describes the same actions and
# parameters as the plist.

STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')
HEADER_RE = re.compile(r"^action (?:v\d+ )?'([^']+)' (sa\w+)\((.*)\)(?:: (\w+))?( \{)?$")
QUESTION_RE = re.compile(r'^#question (\w+) ("(?:[^"\\]|\\.)*") ""$')
CONST_RE = re.compile(r"^\s*const (\w+) = ")
CALL_RE = re.compile(r"\b(sa\w+)\(")


def full_identifier(identifier: str) -> str:
    # Cherri: fewer than 3 dot-parts (or 3 lowercase ones) means is.workflow.actions.<id>.
    parts = identifier.split(".")
    if len(parts) < 3 or (len(parts) == 3 and identifier == identifier.lower()):
        return PREFIX + identifier
    return identifier


def parse_cherri(source: str) -> dict[str, Any]:
    lines = source.splitlines()
    definitions: dict[str, dict[str, Any]] = {}
    questions: list[tuple[str, str]] = []
    defines: dict[str, str] = {}
    body: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        if line.startswith("#define "):
            key, _, value = line.removeprefix("#define ").partition(" ")
            defines[key] = value
            continue
        if line.startswith("#question "):
            match = QUESTION_RE.match(line)
            assert match, line
            questions.append((match.group(1), json.loads(match.group(2))))
            continue
        if line.startswith("action "):
            header = HEADER_RE.match(line)
            assert header, line
            identifier, name, signature, _, block = header.groups()
            extra: dict[str, Any] = {}
            if block:
                block_lines = []
                while lines[i] != "}":
                    block_lines.append(lines[i])
                    i += 1
                i += 1
                extra = json.loads("{" + "\n".join(block_lines) + "}")
            assert name not in definitions, name
            definitions[name] = {
                "identifier": full_identifier(identifier),
                "keys": re.findall(r": '(\w+)'", signature),
                "extra": extra,
            }
            continue
        body.append(line)
    return {"definitions": definitions, "questions": questions, "defines": defines, "body": body}


def check_cherri_shape(source: str) -> dict[str, Any]:
    parsed = parse_cherri(source)
    depth = {"{": 0, "(": 0}
    closing = {"}": "{", ")": "("}
    consts: list[str] = []
    calls: set[str] = set()
    for line in parsed["body"]:
        for literal in STRING_RE.findall(line):
            for name in re.findall(r"\{(\w+)\}", json.loads(literal)):
                assert name in consts, f"{name} used before it's defined: {line}"
        bare = STRING_RE.sub("", line)
        assert '"' not in bare and "'" not in bare, f"unbalanced quote: {line}"
        for ch in bare:
            if ch in depth:
                depth[ch] += 1
            elif ch in closing:
                depth[closing[ch]] -= 1
                assert depth[closing[ch]] >= 0, line
        const = CONST_RE.match(line)
        if const:
            assert const.group(1) not in consts, f"const defined twice: {line}"
            consts.append(const.group(1))
        calls.update(CALL_RE.findall(bare))
    assert depth == {"{": 0, "(": 0}
    assert calls == set(parsed["definitions"])  # every call defined, every definition used
    for qid, _ in parsed["questions"]:
        uses = [line for line in parsed["body"] if re.search(rf"\b{qid}\b", line)]
        assert uses == [f"const {qid.removesuffix('Question')} = text({qid})"]
    assert parsed["defines"]["color"] == "gray"
    assert parsed["defines"]["version"] == "26"
    return parsed


def cherri_identifiers(parsed: dict[str, Any]) -> set[str]:
    found = {d["identifier"] for d in parsed["definitions"].values()}
    body = "\n".join(STRING_RE.sub('""', line) for line in parsed["body"])
    if re.search(r"\btext\(", body):
        found.add(PREFIX + "gettext")
    if "stop()" in body:
        found.add(PREFIX + "exit")
    if re.search(r"(^|= )if ", body, re.MULTILINE):
        found.add(PREFIX + "conditional")
    return found


CHERRI_CASES = {
    "dictate": (SSID, LAN, PUBLIC),
    "dictate-lan-only": (SSID, LAN, None),
    "target": (None, None, None),
    "teach": (SSID, LAN, PUBLIC),
    "teach-lan-only": (SSID, LAN, None),
    "offline": (None, None, None),
}


# Cherri-only actions: the comment and Get Dictionary from Input in front of the pairing text.
PAIRING_DEFINITIONS = {"saComment": "comment", "saGetDictionary": "detect.dictionary"}


@pytest.mark.parametrize(("case", "values"), CHERRI_CASES.items(), ids=CHERRI_CASES.keys())
def test_cherri_matches_the_plist(case, values):
    key = case.removesuffix("-lan-only")
    source = build_cherri(key, *values)
    parsed = check_cherri_shape(source)
    shortcut = build(key, *values)
    assert parsed["defines"]["name"] == SHORTCUT_NAMES[key]
    has_secrets = bool(shortcut["WFWorkflowImportQuestions"])

    # Same actions, and each definition carries that action's exact parameter keys/values.
    pairing_ids = {PREFIX + i for i in PAIRING_DEFINITIONS.values()} if has_secrets else set()
    assert (
        cherri_identifiers(parsed)
        == {a["WFWorkflowActionIdentifier"] for a in actions(shortcut)} | pairing_ids
    )
    for name, definition in parsed["definitions"].items():
        if name in PAIRING_DEFINITIONS:
            assert has_secrets
            assert definition["identifier"] == PREFIX + PAIRING_DEFINITIONS[name]
            continue
        candidates = [
            params(a)
            for a in actions(shortcut)
            if a["WFWorkflowActionIdentifier"] == definition["identifier"]
        ]
        assert any(
            all(p.get(k) == v for k, v in definition["extra"].items())
            and all(k in p for k in definition["keys"])
            for p in candidates
        ), name

    # One import question (the pairing text) where the plist has its three, bound to the Text
    # action right after a bare comment call: Cherri 2.3.0 counts only bare calls when it works
    # out a question's action index, so this is the layout that lands on index 1.
    if not has_secrets:
        assert parsed["questions"] == []
        return
    public = values[2] is not None
    assert [qid for qid, _ in parsed["questions"]] == ["pairingTextQuestion"]
    question = parsed["questions"][0][1]
    assert "python -m shoutabout pair" in question
    assert ("--cloudflare" in question) is public
    head = parsed["body"][: 6 if public else 4]
    assert head[0].startswith("saComment(")
    assert head[1:] == [
        "const pairingText = text(pairingTextQuestion)",
        "const pairing = saGetDictionary(pairingText)",
        'const hubToken = saDictionaryValue(pairing, "token")',
    ] + (
        [
            'const cfClientId = saDictionaryValue(pairing, "cf_client_id")',
            'const cfClientSecret = saDictionaryValue(pairing, "cf_client_secret")',
        ]
        if public
        else []
    )


def test_cherri_contains_the_given_values_only():
    dictate = build_cherri("dictate", SSID, LAN, PUBLIC)
    assert f'wifiName == "{SSID}"' in dictate
    assert f'"{LAN}?target={{target}}"' in dictate
    assert f'"{PUBLIC}?target={{target}}"' in dictate
    teach = build_cherri("teach", SSID, LAN, PUBLIC)
    assert f'"{LAN_VOCAB}"' in teach
    assert f'"{PUBLIC_VOCAB}"' in teach
    assert '{"term": "{newTerm}"}' in teach
    offline = build_cherri("offline", None, None, None)
    for source in (dictate, teach, offline):
        assert "cfast_" not in source
        assert re.search(r"Bearer [^{]", source) is None  # only ever the hubToken constant
    assert "http" not in offline.split("#define", 1)[1]


def test_cherri_escapes_quotes_and_rejects_braces():
    source = build_cherri("dictate", 'My "Home" \\ Net', LAN, None)
    check_cherri_shape(source)
    assert r'wifiName == "My \"Home\" \\ Net"' in source
    with pytest.raises(ValueError, match="Cherri"):
        build_cherri("dictate", "Net{1}", LAN, None)
    with pytest.raises(ValueError):
        build_cherri("teach", "", LAN, None)


def test_cli_format_cherri_writes_sources(home, tmp_path, capsys, no_network):
    out = tmp_path / "out"
    rc = cli.main(
        ["make-shortcuts", "--format", "cherri", "--ssid", "X", "--lan-url", LAN]
        + ["--public-url", PUBLIC, "--out", str(out)]
    )
    assert rc == 0  # no_network: signing would fail the test
    assert sorted(p.name for p in out.iterdir()) == sorted(
        f"{name}.cherri" for name in SHORTCUT_NAMES.values()
    )
    for path in out.iterdir():
        check_cherri_shape(path.read_text("utf-8"))
    printed = capsys.readouterr().out
    assert "haven't been run on an iPhone" in printed
    assert "shoutabout pair --cloudflare" in printed
    assert "playground.cherrilang.org" in printed
    assert "HubSign" not in printed
