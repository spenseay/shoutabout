"""Generate the iPhone shortcuts and sign them.

Four shortcuts:

- **Dictate** (Action Button): records, sends the audio to the hub, copies the text.
- **Dictation Target** (Control Center): flips ``dictation-target.txt`` between pc and phone.
- **Teach ShoutAbout**: asks for a word or phrase and adds it to the hub's vocab (``POST /vocab``).
- **Dictate (offline)**: records and transcribes on the iPhone with Apple's **Transcribe Audio**
  action. No network, no hub, no secrets.

iOS only imports Apple-signed ``.shortcut`` files. Without a Mac, :func:`sign_with_hubsign`
sends the unsigned plist to RoutineHub's HubSign service, which returns a signed file. This
happens once, at setup time; nothing here runs while dictating. :func:`build_cherri` writes the
same shortcuts as Cherri source instead, for the official Cherri compiler or playground to
compile and sign (checked with the Cherri 2.3.0 playground; not yet run on an iPhone).

**No secrets go into the files.** "Dictate" and "Teach ShoutAbout" start with three empty Text
actions (hub token, Cloudflare Client ID and Client Secret) that iOS fills in from *import
questions* when the shortcut is added on the iPhone. The plist (and so HubSign) only ever sees
the actions, the URLs and the home Wi-Fi name. The Cherri source asks a single question instead,
the pairing text from ``python -m shoutabout pair`` (see the Cherri section below for why).

The structure mirrors what was verified on a real iPhone:

- the recording is the raw request body (``WFHTTPBodyType`` ``File``) and the target goes in the
  URL as ``?target=<text of dictation-target.txt>``. A Form body with a file field was silently
  dropped by iOS, so the hub got no file.
- ``status`` is converted with Get Text from Input before ``If Text is "ok"``: comparing a raw
  Dictionary Value with "is" didn't match, and "has any value" is true even for empty values.
- the notification (only shown when ``status`` isn't ``ok``) always starts with literal text,
  because an empty notification body fails with "Please choose a value for each parameter".

Teach and Offline reuse those rules (status compared as Text with "is", literal text first in
every notification); they haven't been run on a real iPhone yet.
"""

from __future__ import annotations

import plistlib
import uuid
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from shoutabout import __version__
from shoutabout.pairing import SECRET_KEYS

DICTATE_NAME = "Dictate"
TARGET_TOGGLE_NAME = "Dictation Target"
TEACH_NAME = "Teach ShoutAbout"
OFFLINE_NAME = "Dictate (offline)"
TARGET_FILE = "dictation-target.txt"  # in iCloud Drive/Shortcuts, shared by both shortcuts

# make-shortcuts --which keys -> shortcut names, in the order they're written.
SHORTCUT_NAMES = {
    "dictate": DICTATE_NAME,
    "target": TARGET_TOGGLE_NAME,
    "teach": TEACH_NAME,
    "offline": OFFLINE_NAME,
}
NEEDS_HUB = frozenset({"dictate", "teach"})  # these need the Wi-Fi name and the hub URLs

HUBSIGN_URL = "https://hubsign.routinehub.services/sign"
HUBSIGN_TIMEOUT_S = 60.0
HUBSIGN_USER_AGENT = f"shoutabout/{__version__}"
SIGNED_MAGIC = b"AEA1"  # Apple Encrypted Archive: what a signed .shortcut file starts with

CLIENT_VERSION = "4033.0.4.3"  # Shortcuts on iOS 26; imports on iOS 27 as well
ICON_COLOR = 3031607807  # Cherri's "gray"
OBJECT_REPLACEMENT = "￼"  # stands in for each variable inside a text field

TOKEN_QUESTION = "Paste the hub token (python -m shoutabout token --qr)"
CF_ID_QUESTION = (
    "Paste the Cloudflare service token Client ID, ending in .access "
    "(copy it, then python -m shoutabout qr --clipboard)"
)
CF_SECRET_QUESTION = (
    "Paste the Cloudflare service token Client Secret "
    "(copy it, then python -m shoutabout qr --clipboard)"
)
# Cherri source asks one question instead: the whole pairing text (python -m shoutabout pair).
PAIRING_QUESTION = (
    "Paste the pairing text (on the PC: python -m shoutabout pair{flag}, scan the QR code, "
    "copy its text)"
)
PAIRING_COMMENT = (
    "ShoutAbout: the Text below is the pairing text pasted when this shortcut was added. It "
    "holds the hub token and the Cloudflare service token, so keep this shortcut to yourself."
)
NO_PUBLIC_MESSAGE = "Not on home Wi-Fi and no public URL configured"
PROBLEM_TITLE = "Dictation problem"
PROBLEM_PREFIX = "Check the PC. "

TEACH_PROMPT = "Word or phrase to teach ShoutAbout"
TEACH_TITLE = "ShoutAbout"
TEACH_ADDED = "added"  # POST /vocab "status": added | exists | invalid
TEACH_EXISTS = "exists"
TEACH_ADDED_PREFIX = "Taught: "
TEACH_EXISTS_PREFIX = "Already in your vocab: "
TEACH_FAILED_PREFIX = "Not added: "

# Apple's on-device "Transcribe Audio" (iOS 17+). The identifier and its one parameter key
# ("audio") come from Cherri's action definitions (actions/text.cherri); not yet run on a phone.
TRANSCRIBE_AUDIO_ACTION = "com.apple.ShortcutsActions.TranscribeAudioAction"
TRANSCRIBE_AUDIO_PARAM = "audio"
OFFLINE_TITLE = "Dictation (offline)"
NOTHING_HEARD = "Nothing heard"

Action = dict[str, Any]
Shortcut = dict[str, Any]
TextPart = str | dict[str, Any]


class ShortcutSignError(Exception):
    """HubSign couldn't sign a shortcut (unreachable, refused, or not a signed file)."""


# ------------------------------------------------------------------ plist encoding


def _uid() -> str:
    return str(uuid.uuid4()).upper()


def _ref(output_uuid: str, name: str) -> dict[str, Any]:
    """A reference to another action's output ("magic variable")."""
    return {"OutputUUID": output_uuid, "OutputName": name, "Type": "ActionOutput"}


def _attach(ref: dict[str, Any]) -> dict[str, Any]:
    """A parameter whose whole value is one variable."""
    return {"Value": ref, "WFSerializationType": "WFTextTokenAttachment"}


def _text(*parts: TextPart) -> dict[str, Any]:
    """A text field mixing literal strings and variables (``WFTextTokenString``).

    Each variable is an object-replacement character in the string; ``attachmentsByRange``
    maps its position, in UTF-16 code units, to the variable.
    """
    text = ""
    attachments: dict[str, dict[str, Any]] = {}
    for part in parts:
        if isinstance(part, dict):
            position = len(text.encode("utf-16-le")) // 2
            attachments[f"{{{position}, 1}}"] = part
            text += OBJECT_REPLACEMENT
        else:
            text += part
    return {
        "Value": {"string": text, "attachmentsByRange": attachments},
        "WFSerializationType": "WFTextTokenString",
    }


def _action(identifier: str, **params: Any) -> Action:
    return _full_action(f"is.workflow.actions.{identifier}", **params)


def _full_action(identifier: str, **params: Any) -> Action:
    """An action by its full identifier (App Intent actions like Transcribe Audio)."""
    return {"WFWorkflowActionIdentifier": identifier, "WFWorkflowActionParameters": params}


def _if(group: str, input_ref: dict[str, Any], condition: int, **value: Any) -> Action:
    """``If <input> <condition> <value>`` (opens a conditional block)."""
    return _action(
        "conditional",
        GroupingIdentifier=group,
        WFControlFlowMode=0,
        WFConditions={
            "Value": {
                "WFActionParameterFilterPrefix": -1,
                "WFActionParameterFilterTemplates": [
                    {
                        "WFCondition": condition,
                        **value,
                        "WFInput": {"Type": "Variable", "Variable": _attach(input_ref)},
                    }
                ],
            },
            "WFSerializationType": "WFContentPredicateTableTemplate",
        },
    )


def _if_is(group: str, input_ref: dict[str, Any], value: str) -> Action:
    """``If <text> is <value>``."""
    return _if(group, input_ref, 4, WFConditionalActionString=value)


def _if_number_is(group: str, input_ref: dict[str, Any], value: int) -> Action:
    """``If <number> is <value>`` (a number condition: ``WFNumberValue``)."""
    return _if(group, input_ref, 4, WFNumberValue=value)


def _otherwise(group: str) -> Action:
    return _action("conditional", GroupingIdentifier=group, WFControlFlowMode=1)


def _end_if(group: str, output_uuid: str) -> Action:
    """``End If``; its output ("If Result") is the last output of whichever branch ran."""
    return _action("conditional", GroupingIdentifier=group, WFControlFlowMode=2, UUID=output_uuid)


def _dict_field(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "Value": {"WFDictionaryFieldValueItems": items},
        "WFSerializationType": "WFDictionaryFieldValue",
    }


def _text_item(key: str, *value: TextPart) -> dict[str, Any]:
    """A Text row of a dictionary field (a header, or a JSON body value)."""
    return {"WFItemType": 0, "WFKey": _text(key), "WFValue": _text(*value)}


_header = _text_item


def _post_file(
    url: tuple[TextPart, ...], headers: list[dict[str, Any]], body: dict[str, Any], uid: str
) -> Action:
    """Get Contents of URL: POST with the file itself as the body (Request Body: File)."""
    return _action(
        "downloadurl",
        UUID=uid,
        WFURL=_text(*url),
        WFHTTPMethod="POST",
        ShowHeaders=True,
        WFHTTPHeaders=_dict_field(headers),
        WFHTTPBodyType="File",
        WFRequestVariable=_attach(body),
    )


def _post_json(
    url: str, headers: list[dict[str, Any]], values: list[dict[str, Any]], uid: str
) -> Action:
    """Get Contents of URL: POST a JSON object (Request Body: JSON)."""
    return _action(
        "downloadurl",
        UUID=uid,
        WFURL=_text(url),
        WFHTTPMethod="POST",
        ShowHeaders=True,
        WFHTTPHeaders=_dict_field(headers),
        WFHTTPBodyType="JSON",
        WFJSONValues=_dict_field(values),
    )


def _notification(title: str, *body: TextPart) -> Action:
    return _action(
        "notification", WFNotificationActionTitle=title, WFNotificationActionBody=_text(*body)
    )


def _dict_value(uid: str, key: str, source: dict[str, Any], name: str) -> Action:
    return _action(
        "getvalueforkey",
        UUID=uid,
        WFGetDictionaryValueType="Value",
        WFDictionaryKey=key,
        WFInput=_attach(source),
        CustomOutputName=name,
    )


def _as_text(uid: str, source: dict[str, Any]) -> Action:
    """Get Text from Input: compare this, not the raw Dictionary Value, with "is"."""
    return _action("detect.text", UUID=uid, WFInput=_attach(source))


def _secret_text(uid: str, name: str) -> Action:
    """An empty Text action that an import question fills in on the iPhone."""
    return _action("gettext", UUID=uid, WFTextActionText="", CustomOutputName=name)


def _question(action_index: int, prompt: str) -> dict[str, Any]:
    return {
        "Category": "Parameter",
        "ActionIndex": action_index,
        "ParameterKey": "WFTextActionText",
        "Text": prompt,
        "DefaultValue": "",
    }


def _read_target(file_uid: str, text_uid: str) -> list[Action]:
    """Get dictation-target.txt from the Shortcuts folder (no error if missing) as Text."""
    return [
        _action(
            "documentpicker.open",
            UUID=file_uid,
            WFGetFilePath=TARGET_FILE,
            WFFileErrorIfNotFound=False,
            WFShowFilePicker=False,
        ),
        _as_text(text_uid, _ref(file_uid, "File")),
    ]


def _shortcut(actions: list[Action], questions: list[dict[str, Any]]) -> Shortcut:
    return {
        "WFWorkflowClientVersion": CLIENT_VERSION,
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": ICON_COLOR},
        "WFWorkflowImportQuestions": questions,
        "WFWorkflowTypes": ["WFWorkflowTypeShowInSearch"],
        "WFWorkflowInputContentItemClasses": [],
        "WFWorkflowHasShortcutInputVariables": False,
        "WFWorkflowActions": actions,
    }


# ------------------------------------------------------------- shared hub parts


class _Secrets:
    """The import-question Text actions at the very start of Dictate and Teach.

    Action 0 is the hub token; with a public URL, actions 1 and 2 are the Cloudflare Client ID
    and Secret. The import questions point at these actions by index, so they must stay first.
    """

    def __init__(self, public: bool) -> None:
        token, cf_id, cf_secret = _uid(), _uid(), _uid()
        self.actions: list[Action] = [_secret_text(token, "Hub token")]
        self.questions = [_question(0, TOKEN_QUESTION)]
        if public:
            self.actions += [
                _secret_text(cf_id, "CF Client ID"),
                _secret_text(cf_secret, "CF Client Secret"),
            ]
            self.questions += [_question(1, CF_ID_QUESTION), _question(2, CF_SECRET_QUESTION)]
        self.bearer = _header("Authorization", "Bearer ", _ref(token, "Hub token"))
        self.cf_headers = [
            _header("CF-Access-Client-Id", _ref(cf_id, "CF Client ID")),
            _header("CF-Access-Client-Secret", _ref(cf_secret, "CF Client Secret")),
            self.bearer,
        ]


def _route(
    ssid: str, lan_request: Action, public_request: Action | None, response: str, title: str
) -> list[Action]:
    """Get the Wi-Fi name; LAN request on home Wi-Fi, else the public one (or stop).

    ``response`` is the End If's UUID: its "If Result" is the reply of whichever request ran.
    Without a public request, the Otherwise branch shows ``NO_PUBLIC_MESSAGE`` and stops, so
    the hub token never goes over plain HTTP to some other network.
    """
    wifi, group = _uid(), _uid()
    actions = [
        _action("getwifi", UUID=wifi, WFNetworkDetailsNetwork="Wi-Fi", WFWiFiDetail="Network Name"),
        _if_is(group, _ref(wifi, "Network Details"), ssid),
        lan_request,
        _otherwise(group),
    ]
    if public_request is not None:
        actions.append(public_request)
    else:
        actions += [_notification(title, NO_PUBLIC_MESSAGE), _action("exit")]
    actions.append(_end_if(group, response))
    return actions


def _check_ssid(ssid: str) -> None:
    if not ssid.strip():
        raise ValueError("the home Wi-Fi name (SSID) is empty")


# ------------------------------------------------------------------------ URLs


def _checked_url(url: str, https_only: bool, example_path: str) -> tuple[str, str, str]:
    """Validate a hub URL; return (scheme, netloc, path without a trailing slash)."""
    url = url.strip()
    parts = urlsplit(url)
    allowed = ("https",) if https_only else ("http", "https")
    if parts.scheme.lower() not in allowed or not parts.netloc:
        scheme = "https://" if https_only else "http:// or https://"
        raise ValueError(f"not a valid URL (expected {scheme}host[:port]{example_path}): {url!r}")
    if "?" in url or "#" in url:
        raise ValueError(f"the URL must not have a query string or fragment: {url!r}")
    return parts.scheme.lower(), parts.netloc, parts.path.rstrip("/")


def transcribe_url(url: str, *, https_only: bool = False) -> str:
    """Check a hub URL and return it ending in ``/transcribe``.

    A bare origin (``http://10.0.0.5:8766``) gets ``/transcribe`` appended. Query strings and
    fragments are rejected because the shortcut appends ``?target=...`` itself.
    """
    scheme, netloc, path = _checked_url(url, https_only, "/transcribe")
    return urlunsplit((scheme, netloc, path or "/transcribe", "", ""))


def vocab_url(url: str, *, https_only: bool = False) -> str:
    """The hub's ``/vocab`` URL from its origin or its ``/transcribe`` (or ``/vocab``) URL."""
    scheme, netloc, path = _checked_url(url, https_only, "")
    for endpoint in ("/transcribe", "/vocab"):
        if path.endswith(endpoint):
            path = path[: -len(endpoint)]
            break
    return urlunsplit((scheme, netloc, path + "/vocab", "", ""))


# ------------------------------------------------------------------- shortcuts


def build_dictate(ssid: str, lan_url: str, public_url: str | None) -> Shortcut:
    """The "Dictate" shortcut (Action Button).

    Reads the target, records, then POSTs the recording to ``lan_url`` when the Wi-Fi name is
    exactly ``ssid``, otherwise to ``public_url`` with the Cloudflare service-token headers.
    The reply's ``text`` goes to the clipboard; a notification appears only when ``status``
    isn't ``ok``. With ``public_url=None`` (LAN-only) the Otherwise branch shows
    "Not on home Wi-Fi and no public URL configured" and stops, and only the hub token is asked
    for on import.
    """
    _check_ssid(ssid)
    lan = transcribe_url(lan_url)
    public = transcribe_url(public_url, https_only=True) if public_url is not None else None

    secrets = _Secrets(public is not None)
    target_file, target = _uid(), _uid()
    audio, lan_request, public_request, response = _uid(), _uid(), _uid(), _uid()
    text, warning, status, detail, status_text = _uid(), _uid(), _uid(), _uid(), _uid()
    status_group = _uid()

    target_ref = _ref(target, "Text")
    audio_ref = _ref(audio, "Recorded Audio")

    # Secrets first: the import questions point at these actions by index.
    actions = list(secrets.actions)
    # pc or phone, written by "Dictation Target". Missing/empty file = pc on the hub.
    actions += _read_target(target_file, target)
    actions.append(
        _action(
            "recordaudio",
            UUID=audio,
            WFRecordingCompression="Normal",
            WFRecordingStart="Immediately",
        )
    )
    lan_post = _post_file((lan + "?target=", target_ref), [secrets.bearer], audio_ref, lan_request)
    public_post = (
        _post_file((public + "?target=", target_ref), secrets.cf_headers, audio_ref, public_request)
        if public is not None
        else None
    )
    actions += _route(ssid, lan_post, public_post, response, "Dictation")

    reply = _ref(response, "If Result")
    actions += [
        _dict_value(text, "text", reply, "Transcript"),
        _dict_value(warning, "warning", reply, "Warning"),
        _action("setclipboard", WFInput=_attach(_ref(text, "Transcript"))),
        # Silent on success: only an exact status "ok" skips the notification.
        _dict_value(status, "status", reply, "Status"),
        _dict_value(detail, "detail", reply, "Error"),
        # Compare as Text: a raw Dictionary Value doesn't match "is ok".
        _as_text(status_text, _ref(status, "Status")),
        _if_is(status_group, _ref(status_text, "Text"), "ok"),
        _otherwise(status_group),
        # Literal text first: an empty body fails with "Please choose a value for each parameter".
        _notification(
            PROBLEM_TITLE,
            PROBLEM_PREFIX,
            _ref(warning, "Warning"),
            " ",
            _ref(detail, "Error"),
        ),
        _end_if(status_group, _uid()),
    ]
    return _shortcut(actions, secrets.questions)


def build_target_toggle() -> Shortcut:
    """The "Dictation Target" shortcut (Control Center): flips dictation-target.txt pc <-> phone."""
    target_file, target, group, result = _uid(), _uid(), _uid(), _uid()
    actions = [
        *_read_target(target_file, target),
        _if_is(group, _ref(target, "Text"), "phone"),
        _action("gettext", UUID=_uid(), WFTextActionText="pc"),
        _otherwise(group),
        _action("gettext", UUID=_uid(), WFTextActionText="phone"),
        _end_if(group, result),
        _action(
            "documentpicker.save",
            WFInput=_attach(_ref(result, "If Result")),
            WFAskWhereToSave=False,
            WFFileDestinationPath=TARGET_FILE,
            WFSaveFileOverwrite=True,
        ),
        _notification("Dictation", "Dictation → ", _ref(result, "If Result")),
    ]
    return _shortcut(actions, [])


def build_teach(ssid: str, lan_url: str, public_url: str | None) -> Shortcut:
    """The "Teach ShoutAbout" shortcut: add a word or phrase to the hub's vocab.

    Asks for text (the keyboard's dictation mic works in the field), then POSTs
    ``{"term": <text>}`` as JSON to the hub's ``/vocab`` with the same Wi-Fi routing, headers
    and import questions as Dictate. ``lan_url``/``public_url`` may be the hub's origin or its
    ``/transcribe`` URL. Exactly one notification: "Taught: <term>" when the reply's ``status``
    is ``added``, "Already in your vocab: <term>" when it's ``exists``, otherwise
    "Not added: <detail>" (an invalid term, or a hub error such as a wrong token).
    """
    _check_ssid(ssid)
    lan = vocab_url(lan_url)
    public = vocab_url(public_url, https_only=True) if public_url is not None else None

    secrets = _Secrets(public is not None)
    ask, lan_request, public_request, response = _uid(), _uid(), _uid(), _uid()
    status, term, detail, status_text = _uid(), _uid(), _uid(), _uid()
    added_group, exists_group = _uid(), _uid()

    body = [_text_item("term", _ref(ask, "Provided Input"))]
    actions = list(secrets.actions)  # first: the import questions point at them by index
    actions.append(
        _action(
            "ask",
            UUID=ask,
            WFAskActionPrompt=TEACH_PROMPT,
            WFInputType="Text",
            WFAllowsMultilineText=False,
        )
    )
    lan_post = _post_json(lan, [secrets.bearer], body, lan_request)
    public_post = (
        _post_json(public, secrets.cf_headers, body, public_request) if public is not None else None
    )
    actions += _route(ssid, lan_post, public_post, response, TEACH_TITLE)

    reply = _ref(response, "If Result")
    status_ref = _ref(status_text, "Text")
    term_ref = _ref(term, "Term")
    actions += [
        _dict_value(status, "status", reply, "Status"),
        _dict_value(term, "term", reply, "Term"),
        _dict_value(detail, "detail", reply, "Error"),
        # Compare as Text, with "is": never "has any value".
        _as_text(status_text, _ref(status, "Status")),
        _if_is(added_group, status_ref, TEACH_ADDED),
        _notification(TEACH_TITLE, TEACH_ADDED_PREFIX, term_ref),
        _otherwise(added_group),
        _if_is(exists_group, status_ref, TEACH_EXISTS),
        _notification(TEACH_TITLE, TEACH_EXISTS_PREFIX, term_ref),
        _otherwise(exists_group),
        # An invalid term (detail "invalid term: ...") or a hub error (401, 503, ...).
        _notification(TEACH_TITLE, TEACH_FAILED_PREFIX, _ref(detail, "Error")),
        _end_if(exists_group, _uid()),
        _end_if(added_group, _uid()),
    ]
    return _shortcut(actions, secrets.questions)


def build_offline() -> Shortcut:
    """The "Dictate (offline)" shortcut: Apple's on-device recognition, no hub at all.

    Record Audio, then **Transcribe Audio** (iOS 17+). An empty transcript (0 characters)
    shows "Nothing heard" and leaves the clipboard alone; otherwise the text is copied,
    silently. No network request, no import questions, no secrets.
    """
    audio, transcript, count, group = _uid(), _uid(), _uid(), _uid()
    transcript_ref = _ref(transcript, "Transcript")
    actions = [
        _action(
            "recordaudio",
            UUID=audio,
            WFRecordingCompression="Normal",
            WFRecordingStart="Immediately",
        ),
        _full_action(
            TRANSCRIBE_AUDIO_ACTION,
            UUID=transcript,
            CustomOutputName="Transcript",
            **{TRANSCRIBE_AUDIO_PARAM: _attach(_ref(audio, "Recorded Audio"))},
        ),
        _action("count", UUID=count, WFCountType="Characters", Input=_attach(transcript_ref)),
        _if_number_is(group, _ref(count, "Count"), 0),
        _notification(OFFLINE_TITLE, NOTHING_HEARD),
        _otherwise(group),
        _action("setclipboard", WFInput=_attach(transcript_ref)),
        _end_if(group, _uid()),
    ]
    return _shortcut(actions, [])


def build(key: str, ssid: str | None, lan_url: str | None, public_url: str | None) -> Shortcut:
    """Build one shortcut by its ``make-shortcuts --which`` key."""
    if key in NEEDS_HUB and (ssid is None or lan_url is None):
        raise ValueError(f"{SHORTCUT_NAMES[key]} needs the home Wi-Fi name and the LAN URL")
    if key == "dictate":
        return build_dictate(ssid or "", lan_url or "", public_url)
    if key == "teach":
        return build_teach(ssid or "", lan_url or "", public_url)
    if key == "target":
        return build_target_toggle()
    if key == "offline":
        return build_offline()
    raise ValueError(f"unknown shortcut {key!r} (choose from {', '.join(SHORTCUT_NAMES)})")


def to_plist_bytes(shortcut: Shortcut) -> bytes:
    """The unsigned shortcut as an XML plist (what HubSign and ``shortcuts sign`` take)."""
    return plistlib.dumps(shortcut, fmt=plistlib.FMT_XML)


# ---------------------------------------------------------------- Cherri source
#
# The same four shortcuts as Cherri source (https://cherrilang.org), for the official Cherri
# compiler or playground to compile and sign. Every Shortcuts action is spelled out with an
# action definition carrying the exact parameters of the plist above, so the compiled result
# should match it action for action. Only text(), stop(), #question, if/else and
# control-flow output (const x = if ...) rely on Cherri's own translation.
#
# One difference: the secrets. Cherri 2.3.0 (what the playground runs) records an import
# question's action index with a counter that skips `const x = action()` lines, so the plist's
# three questions all came out pointing at action 0 (fixed in later Cherri, not yet released).
# So the Cherri source asks a single question, the pairing text from `python -m shoutabout pair`
# (JSON), in a Text action right after a bare comment call: index 1 in every Cherri version.
# Get Dictionary from Input then reads the token and Cloudflare values out of it.
#
# Checked with the Cherri 2.3.0 playground compiler (2026-10-10); not yet run on an iPhone.

CHERRI_HEADER = (
    "// {name}: a ShoutAbout iPhone shortcut, as Cherri source (cherrilang.org).\n"
    "// Generated by python -m shoutabout make-shortcuts --format cherri, shoutabout {version}.\n"
    "// Compiles with Cherri 2.3.0; not yet run on an iPhone. Check the result before use.\n"
    "// No secrets in here. Dictate and Teach ask for the pairing text when they're added.\n"
)

# name -> definition; each file includes only the ones it calls.
CHERRI_DEFINITIONS = {
    "saComment": "action 'comment' saComment(text note: 'WFCommentActionText')",
    "saGetDictionary": (
        "action 'detect.dictionary' saGetDictionary(variable input: 'WFInput'): dictionary"
    ),
    "saGetShortcutsFile": (
        "action 'documentpicker.open' saGetShortcutsFile(text path: 'WFGetFilePath') {\n"
        '    "WFFileErrorIfNotFound": false,\n'
        '    "WFShowFilePicker": false\n'
        "}"
    ),
    "saSaveShortcutsFile": (
        "action 'documentpicker.save' saSaveShortcutsFile("
        "variable content: 'WFInput', text path: 'WFFileDestinationPath') {\n"
        '    "WFAskWhereToSave": false,\n'
        '    "WFSaveFileOverwrite": true\n'
        "}"
    ),
    "saGetText": "action 'detect.text' saGetText(variable input: 'WFInput'): text",
    "saRecordAudio": (
        "action 'recordaudio' saRecordAudio() {\n"
        '    "WFRecordingCompression": "Normal",\n'
        '    "WFRecordingStart": "Immediately"\n'
        "}"
    ),
    "saWiFiNetworkName": (
        "action 'getwifi' saWiFiNetworkName(): text {\n"
        '    "WFNetworkDetailsNetwork": "Wi-Fi",\n'
        '    "WFWiFiDetail": "Network Name"\n'
        "}"
    ),
    "saPostFile": (
        "action 'downloadurl' saPostFile(text url: 'WFURL', variable body: 'WFRequestVariable', "
        "dictionary! headers: 'WFHTTPHeaders') {\n"
        '    "WFHTTPMethod": "POST",\n'
        '    "ShowHeaders": true,\n'
        '    "WFHTTPBodyType": "File"\n'
        "}"
    ),
    "saPostJSON": (
        "action 'downloadurl' saPostJSON(text url: 'WFURL', dictionary! body: 'WFJSONValues', "
        "dictionary! headers: 'WFHTTPHeaders') {\n"
        '    "WFHTTPMethod": "POST",\n'
        '    "ShowHeaders": true,\n'
        '    "WFHTTPBodyType": "JSON"\n'
        "}"
    ),
    "saDictionaryValue": (
        "action 'getvalueforkey' saDictionaryValue("
        "variable source: 'WFInput', text key: 'WFDictionaryKey') {\n"
        '    "WFGetDictionaryValueType": "Value"\n'
        "}"
    ),
    "saCopyToClipboard": "action 'setclipboard' saCopyToClipboard(variable content: 'WFInput')",
    "saShowNotification": (
        "action 'notification' saShowNotification("
        "text body: 'WFNotificationActionBody', text title: 'WFNotificationActionTitle')"
    ),
    "saAskForText": (
        "action 'ask' saAskForText(text prompt: 'WFAskActionPrompt'): text {\n"
        '    "WFInputType": "Text",\n'
        '    "WFAllowsMultilineText": false\n'
        "}"
    ),
    "saCountCharacters": (
        "action 'count' saCountCharacters(variable input: 'Input'): number {\n"
        '    "WFCountType": "Characters"\n'
        "}"
    ),
    "saTranscribeAudio": (
        f"action v17 '{TRANSCRIBE_AUDIO_ACTION}' "
        f"saTranscribeAudio(variable audio: '{TRANSCRIBE_AUDIO_PARAM}'): text"
    ),
}


class _Var(str):
    """A Cherri constant used inside a string (``{name}``)."""


def _cherri_string(*parts: str) -> str:
    """A double-quoted Cherri string; ``_Var`` parts become inline ``{name}`` references.

    Literal text can't hold braces (Cherri reads them as variable references) or line breaks.
    """
    out = []
    for part in parts:
        if isinstance(part, _Var):
            out.append("{" + part + "}")
            continue
        if any(ch in part for ch in "{}\r\n"):
            raise ValueError(f"can't write {part!r} into Cherri source (braces or line breaks)")
        out.append(part.replace("\\", "\\\\").replace('"', '\\"'))
    return '"' + "".join(out) + '"'


def _cherri_dict(items: list[tuple[str, tuple[str, ...]]]) -> str:
    """A one-line Cherri dictionary literal (parsed as JSON, so JSON escapes only)."""
    return "{" + ", ".join(f"{_cherri_string(k)}: {_cherri_string(*v)}" for k, v in items) + "}"


def _cherri_call(name: str, *args: str) -> str:
    return f"{name}({', '.join(args)})"


class _CherriSecrets:
    """Cherri counterpart of :class:`_Secrets`: one import question for the pairing text.

    The comment call must stay first and bare (not ``const x = ...``): Cherri 2.3.0 counts only
    bare calls when it works out the question's action index, so the question lands on index 1.
    """

    def __init__(self, public: bool) -> None:
        token_key, cf_id_key, cf_secret_key = SECRET_KEYS
        question = PAIRING_QUESTION.format(flag=" --cloudflare" if public else "")
        self.question_lines = [
            f"#question pairingTextQuestion {_cherri_string(question)} {_cherri_string('')}"
        ]
        self.lines = [
            _cherri_call("saComment", _cherri_string(PAIRING_COMMENT)),
            "const pairingText = text(pairingTextQuestion)",
            "const pairing = saGetDictionary(pairingText)",
            f"const hubToken = saDictionaryValue(pairing, {_cherri_string(token_key)})",
        ]
        if public:
            self.lines += [
                f"const cfClientId = saDictionaryValue(pairing, {_cherri_string(cf_id_key)})",
                "const cfClientSecret = "
                f"saDictionaryValue(pairing, {_cherri_string(cf_secret_key)})",
            ]
        bearer = ("Authorization", ("Bearer ", _Var("hubToken")))
        self.lan_headers = _cherri_dict([bearer])
        self.cf_headers = _cherri_dict(
            [
                ("CF-Access-Client-Id", (_Var("cfClientId"),)),
                ("CF-Access-Client-Secret", (_Var("cfClientSecret"),)),
                bearer,
            ]
        )


def _cherri_route(ssid: str, lan_call: str, public_call: str | None, title: str) -> list[str]:
    lines = [
        "const wifiName = saWiFiNetworkName()",
        f"const reply = if wifiName == {_cherri_string(ssid)} {{",
        f"    {lan_call}",
        "} else {",
    ]
    if public_call is not None:
        lines.append(f"    {public_call}")
    else:
        notify = _cherri_call(
            "saShowNotification", _cherri_string(NO_PUBLIC_MESSAGE), _cherri_string(title)
        )
        lines += [f"    {notify}", "    stop()"]
    lines.append("}")
    return lines


def _cherri_target_lines() -> list[str]:
    return [
        f"const targetFile = saGetShortcutsFile({_cherri_string(TARGET_FILE)})",
        "const target = saGetText(targetFile)",
    ]


def _cherri_file(name: str, questions: list[str], body: list[str]) -> str:
    used = [d for n, d in CHERRI_DEFINITIONS.items() if any(f"{n}(" in line for line in body)]
    parts = [
        CHERRI_HEADER.format(name=name, version=__version__),
        f"#define name {name}\n#define color gray\n#define version 26\n",
    ]
    if questions:
        parts.append("\n".join(questions) + "\n")
    parts.append("// Shortcuts actions with their exact parameters.\n" + "\n\n".join(used) + "\n")
    parts.append("\n".join(body) + "\n")
    return "\n".join(parts)


def dictate_cherri(ssid: str, lan_url: str, public_url: str | None) -> str:
    """:func:`build_dictate` as Cherri source."""
    _check_ssid(ssid)
    lan = transcribe_url(lan_url)
    public = transcribe_url(public_url, https_only=True) if public_url is not None else None
    secrets = _CherriSecrets(public is not None)

    def post(url: str, headers: str) -> str:
        return _cherri_call(
            "saPostFile", _cherri_string(url, "?target=", _Var("target")), "recording", headers
        )

    body = [*secrets.lines, *_cherri_target_lines(), "const recording = saRecordAudio()"]
    body += _cherri_route(
        ssid,
        post(lan, secrets.lan_headers),
        post(public, secrets.cf_headers) if public is not None else None,
        "Dictation",
    )
    notify = _cherri_call(
        "saShowNotification",
        _cherri_string(PROBLEM_PREFIX, _Var("replyWarning"), " ", _Var("replyDetail")),
        _cherri_string(PROBLEM_TITLE),
    )
    body += [
        'const transcript = saDictionaryValue(reply, "text")',
        'const replyWarning = saDictionaryValue(reply, "warning")',
        "saCopyToClipboard(transcript)",
        'const replyStatus = saDictionaryValue(reply, "status")',
        'const replyDetail = saDictionaryValue(reply, "detail")',
        "const statusText = saGetText(replyStatus)",
        'if statusText == "ok" {',
        "    // Silent on success.",
        "} else {",
        f"    {notify}",
        "}",
    ]
    return _cherri_file(DICTATE_NAME, secrets.question_lines, body)


def target_toggle_cherri() -> str:
    """:func:`build_target_toggle` as Cherri source."""
    notify = _cherri_call(
        "saShowNotification",
        _cherri_string("Dictation → ", _Var("newTarget")),
        _cherri_string("Dictation"),
    )
    body = [
        *_cherri_target_lines(),
        'const newTarget = if target == "phone" {',
        '    text("pc")',
        "} else {",
        '    text("phone")',
        "}",
        f"saSaveShortcutsFile(newTarget, {_cherri_string(TARGET_FILE)})",
        notify,
    ]
    return _cherri_file(TARGET_TOGGLE_NAME, [], body)


def teach_cherri(ssid: str, lan_url: str, public_url: str | None) -> str:
    """:func:`build_teach` as Cherri source."""
    _check_ssid(ssid)
    lan = vocab_url(lan_url)
    public = vocab_url(public_url, https_only=True) if public_url is not None else None
    secrets = _CherriSecrets(public is not None)
    json_body = _cherri_dict([("term", (_Var("newTerm"),))])

    def post(url: str, headers: str) -> str:
        return _cherri_call("saPostJSON", _cherri_string(url), json_body, headers)

    def notify(*text: str) -> str:
        return _cherri_call(
            "saShowNotification", _cherri_string(*text), _cherri_string(TEACH_TITLE)
        )

    body = [*secrets.lines, f"const newTerm = saAskForText({_cherri_string(TEACH_PROMPT)})"]
    body += _cherri_route(
        ssid,
        post(lan, secrets.lan_headers),
        post(public, secrets.cf_headers) if public is not None else None,
        TEACH_TITLE,
    )
    body += [
        'const replyStatus = saDictionaryValue(reply, "status")',
        'const taughtTerm = saDictionaryValue(reply, "term")',
        'const replyDetail = saDictionaryValue(reply, "detail")',
        "const statusText = saGetText(replyStatus)",
        f"if statusText == {_cherri_string(TEACH_ADDED)} {{",
        f"    {notify(TEACH_ADDED_PREFIX, _Var('taughtTerm'))}",
        "} else {",
        f"    if statusText == {_cherri_string(TEACH_EXISTS)} {{",
        f"        {notify(TEACH_EXISTS_PREFIX, _Var('taughtTerm'))}",
        "    } else {",
        f"        {notify(TEACH_FAILED_PREFIX, _Var('replyDetail'))}",
        "    }",
        "}",
    ]
    return _cherri_file(TEACH_NAME, secrets.question_lines, body)


def offline_cherri() -> str:
    """:func:`build_offline` as Cherri source."""
    notify = _cherri_call(
        "saShowNotification", _cherri_string(NOTHING_HEARD), _cherri_string(OFFLINE_TITLE)
    )
    body = [
        "const recording = saRecordAudio()",
        "const transcript = saTranscribeAudio(recording)",
        "const characterCount = saCountCharacters(transcript)",
        "if characterCount == 0 {",
        f"    {notify}",
        "} else {",
        "    saCopyToClipboard(transcript)",
        "}",
    ]
    return _cherri_file(OFFLINE_NAME, [], body)


def build_cherri(key: str, ssid: str | None, lan_url: str | None, public_url: str | None) -> str:
    """One shortcut as Cherri source, by its ``make-shortcuts --which`` key."""
    if key in NEEDS_HUB and (ssid is None or lan_url is None):
        raise ValueError(f"{SHORTCUT_NAMES[key]} needs the home Wi-Fi name and the LAN URL")
    if key == "dictate":
        return dictate_cherri(ssid or "", lan_url or "", public_url)
    if key == "teach":
        return teach_cherri(ssid or "", lan_url or "", public_url)
    if key == "target":
        return target_toggle_cherri()
    if key == "offline":
        return offline_cherri()
    raise ValueError(f"unknown shortcut {key!r} (choose from {', '.join(SHORTCUT_NAMES)})")


# --------------------------------------------------------------------- signing


def _snippet(response: httpx.Response, limit: int = 200) -> str:
    text = " ".join(response.text.split())
    return text[:limit] + ("..." if len(text) > limit else "")


def sign_with_hubsign(name: str, plist_xml: bytes, client: httpx.Client | None = None) -> bytes:
    """Sign an unsigned shortcut plist with RoutineHub's HubSign; return the signed file.

    Only the shortcut structure is sent (``{"shortcutName", "shortcut"}``). Raises
    :class:`ShortcutSignError` unless HubSign answers 200 with a signed (``AEA1``) file.
    """
    payload = {"shortcutName": name, "shortcut": plist_xml.decode("utf-8")}
    headers = {"User-Agent": HUBSIGN_USER_AGENT}
    http = client if client is not None else httpx.Client()
    try:
        response = http.post(HUBSIGN_URL, json=payload, headers=headers, timeout=HUBSIGN_TIMEOUT_S)
    except httpx.HTTPError as exc:
        raise ShortcutSignError(f"couldn't reach HubSign ({type(exc).__name__}: {exc})") from exc
    finally:
        if client is None:
            http.close()
    if response.is_redirect:
        location = response.headers.get("location", "?")
        raise ShortcutSignError(
            f"HubSign answered HTTP {response.status_code}, redirecting to {location} instead of "
            "signing (it may only accept requests from clients it knows)"
        )
    if response.status_code != 200:
        detail = _snippet(response)
        raise ShortcutSignError(
            f"HubSign answered HTTP {response.status_code}" + (f": {detail}" if detail else "")
        )
    if not response.content.startswith(SIGNED_MAGIC):
        raise ShortcutSignError(
            f"HubSign's reply isn't a signed shortcut ({len(response.content)} bytes, "
            f"doesn't start with {SIGNED_MAGIC.decode()})"
        )
    return response.content
