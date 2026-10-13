"""Recent: the last few dictations and failed recordings, in memory only (the store, the
pipeline / server / dictation wiring), the Recent page's model (no Tk), and a real Tk test
of the page keeping itself current (--run-windows)."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from fakes import FakeCleaner, FakeEngine, FakeInjector, FakeVocabStore, fake_decode
from shoutabout import recent_model as rm
from shoutabout.config import Config
from shoutabout.engine.base import EngineError
from shoutabout.models import CleanupResult, Timings, TranscribeResult
from shoutabout.pipeline import Pipeline
from shoutabout.recent import KEEP, Recent, items_from
from shoutabout.state import HubState
from v2_fakes import make_services

TZ = timezone(timedelta(hours=-7))
NOW = datetime(2026, 10, 10, 15, 30, tzinfo=TZ)


def result(text: str = "Hello there.", **kw) -> TranscribeResult:
    data = dict(
        text=text,
        raw_text=text.lower(),
        target="pc",
        route="local",
        pasted=True,
        warning=None,
        cleanup=CleanupResult(text=text, status="disabled"),
        timings=Timings(),
        audio_seconds=2.0,
        request_id="abc123abc123",
        extra={},
    )
    data.update(kw)
    return TranscribeResult(**data)


# ------------------------------------------------------------------- the store


def test_keeps_only_the_newest_three_meant_for_this_computer():
    recent = Recent()
    assert KEEP == 3 and recent.items() == [] and recent.version == 0
    for i in range(5):
        recent.add(result(f"Dictation {i}."))
    assert [i.text for i in recent.items()] == ["Dictation 4.", "Dictation 3.", "Dictation 2."]
    # Text only the phone got back never landed here; nothing heard is nothing to keep.
    assert recent.add(result("For the phone.", target="phone")) is None
    assert recent.add(result("   ")) is None
    assert len(recent.items()) == 3 and recent.version == 5


def test_items_say_where_they_came_from_and_if_they_were_pasted():
    recent = Recent()
    phone = recent.add(result("Pick up milk.", route="lan", extra={"source": "Sam's iPhone"}))
    missed = recent.add(result("Locked out.", pasted=False, warning="The PC is locked."))
    later = recent.add(result("Saved.", extra={"recovered": True}))
    assert (phone.source, phone.route) == ("Sam's iPhone", "lan")
    assert (missed.pasted, missed.warning) == (False, "The PC is locked.")
    assert later.recovered and phone.id != missed.id != later.id
    assert " ".join(result("a  \n b").text.split()) == "a b"
    assert recent.add(result("a  \n b")).text == "a b"  # one line, as pasted


def test_listeners_hear_every_change():
    recent, heard = Recent(), []
    recent.subscribe(heard.append)
    recent.add(result())
    item = recent.keep_failed(np.zeros(1600, np.float32), error="boom")
    recent.remove_failed(item.id)
    assert heard == [1, 2, 3]
    recent.unsubscribe(heard.append)
    recent.add(result())
    assert heard == [1, 2, 3] and recent.version == 4


def test_failed_recordings_are_kept_in_memory_and_tried_again():
    recent = Recent()
    samples = np.linspace(-0.5, 0.5, 16_000 * 2, dtype=np.float32)
    item = recent.keep_failed(samples, route="lan", source="Sam's iPhone", error="CUDA  oom\n")
    assert (item.seconds, item.error, item.source) == (2.0, "CUDA oom", "Sam's iPhone")
    back = recent.samples(item.id)
    assert back.dtype == np.float32 and np.allclose(back, samples, atol=1e-4)
    assert recent.get_failed(item.id) == item and recent.failed() == [item]
    for _ in range(4):
        recent.keep_failed(np.ones(160, np.float32))
    assert len(recent.failed()) == 3 and recent.samples(item.id) is None  # the oldest went
    assert recent.keep_failed(np.zeros(0, np.float32)) is None
    newest = recent.failed()[0]
    assert recent.remove_failed(newest.id) and not recent.remove_failed(newest.id)


def test_snapshot_crosses_a_link_as_plain_values():
    import json

    recent = Recent()
    recent.add(result("One."))
    recent.keep_failed(np.ones(1600, np.float32), error="boom")
    snap = json.loads(json.dumps(recent.snapshot()))  # what the Mac link carries
    items, failed = items_from(snap)
    assert [i.text for i in items] == ["One."] and failed[0].error == "boom"
    assert "audio" not in json.dumps(snap)
    assert items_from(None) == ([], [])
    assert items_from({"items": [{"bad": 1}], "failed": ["junk"]}) == ([], [])


# -------------------------------------------------------- pipeline / server / hub


def pipeline_with(recent, engine=None) -> Pipeline:
    return Pipeline(
        config=Config(),
        engine=engine or FakeEngine(text="written out"),
        vocab=FakeVocabStore(),
        cleaner=FakeCleaner(),
        injector=FakeInjector(),
        state=HubState(),
        recent=recent,
        decode=fake_decode,
    )


def test_the_pipeline_keeps_results_in_memory_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    recent = Recent()
    p = pipeline_with(recent)
    p.process_audio(np.zeros(16_000, np.float32), target="pc", route="local")
    p.process_file(tmp_path / "a.m4a", target="pc", route="lan", extra={"source": "Sam's"})
    assert [(i.route, i.source) for i in recent.items()] == [("lan", "Sam's"), ("local", None)]
    assert list(tmp_path.iterdir()) == []  # nothing on disk
    failing = pipeline_with(recent, FakeEngine(fail=EngineError("x")))
    with pytest.raises(EngineError):
        failing.process_audio(np.zeros(1600, np.float32))
    assert len(recent.items()) == 2  # a failure isn't a result


def test_try_again_writes_out_a_kept_recording_without_pasting():
    recent = Recent()
    p = pipeline_with(recent)
    item = recent.keep_failed(np.full(16_000, 0.1, np.float32), error="boom")
    out = p.process_audio(
        recent.samples(item.id),
        target="pc",
        route=item.route,
        deliver=False,
        extra={"source": item.source, "recovered": True},
    )
    assert out.text == "written out" and p.injector.pasted == []  # never pasted
    (back,) = recent.items()
    assert back.recovered and back.text == "written out"


def test_phone_dictations_and_pastes_say_which_phone(tmp_path):
    from test_server_v2 import HUB_TOKEN, Hub, bearer

    (tmp_path / "vocab.txt").write_text("x\n", "utf-8")
    hub = Hub(tmp_path)
    hub.ctx.recent = recent = Recent()
    hub.ctx.pipeline.recent = recent
    token = hub.pair(name="Sam's iPhone")
    c = hub.client()
    for t in (token, HUB_TOKEN):
        r = c.post(
            "/transcribe",
            headers={**bearer(t), "Content-Type": "audio/x-m4a"},
            content=b"audio",
            params={"target": "pc"},
        )
        assert r.status_code == 200
    r = c.post("/paste", headers=bearer(token), json={"text": "typed on the phone"})
    assert r.status_code == 200
    assert [i.source for i in recent.items()] == ["Sam's iPhone", None, "Sam's iPhone"]
    assert recent.items()[0].text == "typed on the phone"


def test_a_failed_upload_is_kept_only_when_the_engine_failed(tmp_path):
    from shoutabout.audio import AudioError
    from test_server_v2 import Hub, bearer

    (tmp_path / "vocab.txt").write_text("x\n", "utf-8")
    hub = Hub(tmp_path)
    hub.ctx.recent = recent = Recent()
    token = hub.pair(name="Sam's iPhone")
    c = hub.client()

    def upload():
        return c.post(
            "/transcribe",
            headers={**bearer(token), "Content-Type": "audio/x-m4a"},
            content=b"the phone's audio",
            params={"target": "pc"},
        )

    hub.ctx.pipeline.engine = FakeEngine(fail=EngineError("CUDA out of memory"))
    assert upload().status_code == 503
    (item,) = recent.failed()
    assert (item.route, item.source) == ("lan", "Sam's iPhone")
    assert recent.samples(item.id) is not None  # decoded, in memory
    assert not any(tmp_path.glob("**/*.wav")) and not (tmp_path / "failed").exists()
    hub.ctx.pipeline.engine = FakeEngine(text="ok")

    def bad_audio(_path, _max=None):
        raise AudioError("not audio")

    hub.ctx.pipeline._decode = bad_audio
    assert upload().status_code == 415
    assert len(recent.failed()) == 1  # bad audio: trying again wouldn't help


# ------------------------------------------------------------------- the model


def services_with(tmp_path, recent=None, **kw):
    svc = make_services(tmp_path, with_pairing=False, **kw)
    svc.recent = recent
    return svc


def test_labels():
    assert rm.when_label("2026-10-10T14:03:00-07:00", NOW) == "14:03"
    assert rm.when_label("2026-10-09T09:12:00-07:00", NOW) == "Yesterday 9:12"
    assert rm.when_label("2026-10-03T14:03:00-07:00", NOW) == "3 Oct 14:03"
    assert rm.when_label("nope", NOW) == ""
    assert rm.source_label("local", None, "win32") == "This PC"
    assert rm.source_label("local", None, "darwin") == "This Mac"
    assert rm.source_label("lan", "Sam's iPhone") == "Sam's iPhone"
    assert rm.source_label("tunnel", None) == "Phone"
    assert (rm.length_label(9.6), rm.length_label(125), rm.length_label(None)) == (
        "10 s",
        "2 min 05 s",
        "",
    )


def test_view_lists_newest_first_with_notes(tmp_path):
    recent = Recent()
    recent.add(result("First."))
    recent.add(result("Missed.", pasted=False, warning="locked"))
    recent.add(result("Later.", extra={"recovered": True}))
    recent.keep_failed(np.ones(16_000 * 3, np.float32), error="boom")
    model = rm.RecentModel(services_with(tmp_path, recent))
    assert model.view().loading
    model.load()
    view = model.view()
    assert [(e.text, e.note) for e in view.entries] == [
        ("Later.", "Written out later"),
        ("Missed.", "Not pasted"),
        ("First.", ""),
    ]
    assert view.entries[0].source == rm.source_label("local", None)
    (failed,) = view.failed
    assert (failed.length, failed.error) == ("3 s", "boom")
    assert not view.can_retry and view.empty_text == ""


def test_nothing_yet_and_no_hub(tmp_path):
    model = rm.RecentModel(services_with(tmp_path, Recent()))
    model.load()
    assert model.view().empty_text == rm.EMPTY_TEXT
    standalone = rm.RecentModel(services_with(tmp_path / "x", None))  # no hub here
    assert standalone.stamp() is None
    standalone.load()
    assert standalone.view().empty_text == rm.EMPTY_TEXT


def test_stamp_follows_the_hub(tmp_path):
    recent = Recent()
    model = rm.RecentModel(services_with(tmp_path, recent))
    before = model.stamp()
    recent.add(result())
    assert model.stamp() != before


def test_fix_a_word_teaches_and_corrects(tmp_path):
    from shoutabout.vocab import read_entries

    copied: list[str] = []
    recent = Recent()
    recent.add(result("Ask Sophia to open cloud code."))
    model = rm.RecentModel(services_with(tmp_path, recent, copy_text=copied.append))
    model.load()
    key = model.items[0].id
    vocab = model.svc.paths.vocab_file

    assert model.fix_word(key, "Sophia", " Sofia ") == "Ask Sofia to open cloud code."
    assert model.message == 'Saved. "Sophia" will come out "Sofia" from now on.'
    assert not model.error
    assert read_entries(vocab)[0] == "Sofia"  # a word: it sounds the same
    assert model.view().entries[0].text == "Ask Sofia to open cloud code."

    fixed = model.fix_word(key, "cloud code", "Claude Code")
    assert fixed == "Ask Sofia to open Claude Code."
    assert read_entries(vocab)[0] == "cloud code -> Claude Code"  # can't tell by the sound
    assert model.copy(key) and copied == ["Ask Sofia to open Claude Code."]


def test_fix_a_word_keeps_one_replacement_per_phrase(tmp_path):
    from shoutabout.vocab import read_entries

    recent = Recent()
    recent.add(result("omw, see you."))
    recent.add(result("omw again."))
    model = rm.RecentModel(services_with(tmp_path, recent))
    model.load()
    newer, older = (i.id for i in model.items)
    assert model.fix_word(older, "omw", "on my way") == "on my way, see you."
    assert model.fix_word(newer, "omw", "oh my word") is None
    assert model.message == '"omw" already turns into "on my way". Change it on Teach words.'
    assert model.view().entries[0].text == "omw again."  # left as it was
    assert read_entries(model.svc.paths.vocab_file)[0] == "omw -> on my way"


def test_fix_a_word_needs_both_words(tmp_path):
    recent = Recent()
    recent.add(result("Hello there."))
    model = rm.RecentModel(services_with(tmp_path, recent))
    model.load()
    key = model.items[0].id
    assert model.fix_word(key, "", "Sofia") is None and model.error
    assert model.fix_word(key, "there", "  ") is None and model.error
    assert model.fix_word(key, "there", "there") is None and "same" in model.message
    assert model.fix_word("gone", "a", "b") is None and model.error
    assert model.fix_word(key, "there", "#hash") is None and "comment" in model.message


def test_copy_retry_and_delete(tmp_path):
    copied: list[str] = []
    recent = Recent()
    recent.add(result("Copy me."))
    item = recent.keep_failed(np.ones(1600, np.float32))
    svc = services_with(tmp_path, recent, copy_text=copied.append)
    svc.retry_failed = lambda item_id: "recovered text"
    model = rm.RecentModel(svc)
    model.load()
    assert model.copy(model.items[0].id) and copied == ["Copy me."]
    assert model.message == rm.COPIED
    assert not model.copy("nope") and model.error
    assert model.retry(item.id) and model.message == rm.RETRIED and not model.busy
    svc.retry_failed = lambda item_id: ""
    assert not model.retry(item.id) and "No words" in model.message

    def boom(_id):
        raise RuntimeError("CUDA again")

    svc.retry_failed = boom
    assert not model.retry(item.id) and "CUDA again" in model.message
    svc.retry_failed = None
    assert not model.retry(item.id) and model.message == rm.RETRY_NEEDS_HUB
    assert model.delete_failed(item.id) and recent.failed() == []
    assert not model.delete_failed(item.id) and model.error


# ---------------------------------------------------------------- the page (Tk)


def _pump(root, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.02)


@pytest.fixture
def tk_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display: {exc}")
    root.geometry("700x420+40+40")
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass


@pytest.mark.windows
def test_the_page_shows_a_new_dictation_by_itself(tmp_path, tk_root):
    """The owner's report: with the page open, a new dictation must show up at the top
    within about a second, without losing a selection or where the list was scrolled to."""
    import tkinter as tk

    from shoutabout.recent_ui import RecentPage

    recent = Recent(keep=3)
    long = "word " * 220  # a long dictation: the list has to scroll
    recent.add(result("Oldest. " + long))
    recent.add(result("Middle. " + long))
    frame = tk.Frame(tk_root)
    frame.pack(fill="both", expand=True)
    page = RecentPage(frame, rm.RecentModel(services_with(tmp_path, recent)))
    _pump(tk_root, 0.6)
    texts = page._texts
    assert len(texts) == 2
    middle_key = recent.items()[0].id
    middle = texts[middle_key]
    middle.tag_add("sel", "1.0", "1.6")  # the person selected "Middle"
    assert page.scrollbar.winfo_ismapped()
    page.canvas.yview_moveto(0.5)  # ...and scrolled down
    _pump(tk_root, 0.2)
    seen_before = page.canvas.canvasy(0)
    middle_y_before = middle.winfo_rooty()

    # A dictation finishes on another thread (as the pipeline does).
    t = threading.Thread(target=recent.add, args=(result("Newest dictation."),))
    t.start()
    t.join()
    _pump(tk_root, 1.0)
    keys = [i.id for i in recent.items()]
    assert list(page._cards) and set(page._texts) == set(keys)
    newest = page._texts[keys[0]]
    assert newest.get("1.0", "end").strip() == "Newest dictation."
    # The same widget, with the selection still in it.
    assert page._texts[middle_key] is middle
    assert middle.get("sel.first", "sel.last") == "Middle"
    # Still looking at the same part of the list: the new card arrived above it.
    assert page.canvas.canvasy(0) > seen_before
    assert abs(middle.winfo_rooty() - middle_y_before) <= 3

    # Scrolled to the top, the new one is the first thing in view.
    page.canvas.yview_moveto(0)
    recent.add(result("Even newer."))
    _pump(tk_root, 1.0)
    assert page.canvas.canvasy(0) == 0
    first = page._texts[recent.items()[0].id]
    assert first.get("1.0", "end").strip() == "Even newer."
    assert len(page._texts) == 3  # the oldest dropped off: only three are kept
    page.close()


@pytest.mark.windows
def test_the_page_tries_a_failed_recording_again(tmp_path, tk_root):
    import tkinter as tk

    from shoutabout.recent_ui import RecentPage

    recent = Recent()
    item = recent.keep_failed(np.ones(16_000, np.float32), error="CUDA out of memory")
    svc = services_with(tmp_path, recent)

    def retry(item_id):
        recent.remove_failed(item_id)
        recent.add(result("Back from the dead.", extra={"recovered": True}))
        return "Back from the dead."

    svc.retry_failed = retry
    page = RecentPage(tk.Frame(tk_root), rm.RecentModel(svc))
    page.win.pack(fill="both", expand=True)
    _pump(tk_root, 0.5)
    assert page.failed_box.winfo_children()
    page._retry(item.id)
    _pump(tk_root, 1.0)
    assert not page.failed_box.winfo_children()
    (text,) = page._texts.values()
    assert text.get("1.0", "end").strip() == "Back from the dead."
    assert page.model.message == rm.RETRIED
    page.close()


def test_page_import_is_headless():
    import subprocess
    import sys

    code = "import sys, shoutabout.recent_ui; print('tkinter' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_remote_recent_follows_hub_events():
    """On a Mac the page lives in the window process: the hub's events bump the stamp."""
    from shoutabout.mac.ui_child import RemoteRecent

    calls = []
    link = SimpleNamespace(call=lambda op, **a: calls.append((op, a)) or {"items": []})
    remote = RemoteRecent(link)
    assert remote.version == 0
    remote.changed(7)
    assert remote.version == 1
    assert remote.snapshot() == {"items": []}
    remote.remove_failed("f1")
    assert calls == [("recent", {}), ("remove_failed", {"id": "f1"})]


@pytest.mark.windows
def test_the_page_fixes_a_word(tmp_path, tk_root):
    """Select the wrong word, Fix a word, type the right one, Save: the card is corrected
    and the word is in vocab.txt for next time."""
    import tkinter as tk

    from shoutabout.recent_ui import RecentPage
    from shoutabout.vocab import read_entries

    recent = Recent()
    recent.add(result("I talked to Sophia today."))
    frame = tk.Frame(tk_root)
    frame.pack(fill="both", expand=True)
    model = rm.RecentModel(services_with(tmp_path, recent))
    page = RecentPage(frame, model)
    _pump(tk_root, 0.4)
    key = recent.items()[0].id
    text = page._texts[key]
    text.tag_add("sel", "1.12", "1.18")  # "Sophia"
    page._open_fix(key)
    _row, wrong, right = page._fix_rows[key]
    assert wrong.get() == "Sophia"
    right.insert(0, "Sofia")
    page._save_fix(key)
    _pump(tk_root, 0.5)
    assert text.get("1.0", "end").strip() == "I talked to Sofia today."
    assert key not in page._fix_rows
    assert page.status["text"] == 'Saved. "Sophia" will come out "Sofia" from now on.'
    assert read_entries(model.svc.paths.vocab_file)[0] == "Sofia"
    # Cancel closes the row without saving.
    page._open_fix(key)
    page._close_fix(key)
    assert key not in page._fix_rows
    page.close()
