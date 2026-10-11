"""History: the records (source, recovered, reading back, clearing), the kept recordings of
failed transcriptions, the History window's model (no Tk), the hub/server wiring, and a
real Tk smoke test (--run-windows)."""

from __future__ import annotations

import json
import time
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from fakes import FakeEngine
from shoutabout import history_model as hm
from shoutabout.config_edit import update_config
from shoutabout.engine.base import EngineError
from shoutabout.failed_audio import FailedRecordings
from shoutabout.history import HistoryLog, build_record, clear_history_file, read_history
from shoutabout.models import CleanupResult, Timings, TranscribeResult
from v2_fakes import make_services

TZ = timezone(timedelta(hours=-7))
NOW = datetime(2026, 10, 10, 15, 30, tzinfo=TZ)


def result(**kw) -> TranscribeResult:
    data = dict(
        text="Hello there.",
        raw_text="hello there",
        target="pc",
        route="local",
        pasted=True,
        warning=None,
        cleanup=CleanupResult(text="Hello there.", status="disabled"),
        timings=Timings(),
        audio_seconds=2.0,
        request_id="abc123abc123",
        extra={},
    )
    data.update(kw)
    return TranscribeResult(**data)


def write_history(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), "utf-8")


def rec(i: int, text: str, **kw) -> dict:
    base = {
        "ts": (NOW - timedelta(minutes=10 * i)).isoformat(timespec="seconds"),
        "id": f"id{i:04d}",
        "route": "local",
        "target": "pc",
        "raw_text": text.lower(),
        "text": text,
        "pasted": True,
        "warning": None,
        "source": None,
    }
    base.update(kw)
    return base


# ------------------------------------------------------------------- records


def test_records_say_where_they_came_from_and_if_recovered():
    r = build_record(result(route="lan", extra={"source": "Sam's iPhone"}), ts=NOW)
    assert r["source"] == "Sam's iPhone" and "recovered" not in r
    r = build_record(result(extra={"recovered": True, "source": None}), ts=NOW)
    assert r["source"] is None and r["recovered"] is True


def test_read_history_newest_first_with_a_limit(tmp_path):
    path = tmp_path / "history.jsonl"
    assert read_history(path) == []  # no file yet
    lines = [json.dumps({"id": str(i)}) for i in range(10)]
    lines.insert(3, "not json {")
    lines.insert(5, "[1, 2]")
    path.write_text("\n".join(lines) + "\n\n", "utf-8")
    assert [r["id"] for r in read_history(path)] == [str(i) for i in reversed(range(10))]
    assert [r["id"] for r in read_history(path, 3)] == ["9", "8", "7"]


def test_clear_history(tmp_path):
    log = HistoryLog(tmp_path / "history.jsonl", tmp_path / "audio", False)
    log.append(result())
    assert log.path.exists()
    log.clear()
    assert not log.path.exists()
    log.clear()  # nothing there: fine
    log.append(result(text="after"))  # and writing goes on
    assert read_history(log.path)[0]["text"] == "after"
    clear_history_file(log.path)
    assert not log.path.exists()


# ----------------------------------------------------------- failed recordings


def test_keep_samples_writes_a_wav_and_its_details(tmp_path):
    failed = FailedRecordings(tmp_path / "failed")
    audio = np.sin(np.linspace(0, 200, 16_000 * 3)).astype(np.float32) * 0.5
    item = failed.keep_samples(audio, error="CUDA out of\n memory")
    assert item is not None and item.route == "local" and item.source is None
    assert item.seconds == pytest.approx(3.0) and item.error == "CUDA out of memory"
    with wave.open(str(item.audio)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16_000)
        assert w.getnframes() == 16_000 * 3
    meta = json.loads((tmp_path / "failed" / f"{item.id}.json").read_text("utf-8"))
    assert set(meta) == {"ts", "route", "source", "seconds", "error", "audio"}
    assert failed.get(item.id) == item
    assert failed.keep_samples(np.zeros(0, np.float32)) is None


def test_keep_file_copies_the_upload(tmp_path):
    src = tmp_path / "upload.m4a"
    src.write_bytes(b"fake m4a")
    failed = FailedRecordings(tmp_path / "failed")
    item = failed.keep_file(src, route="lan", source="Sam's iPhone", error="engine broke")
    assert item.audio.suffix == ".m4a" and item.audio.read_bytes() == b"fake m4a"
    assert (item.route, item.source, item.seconds) == ("lan", "Sam's iPhone", None)
    odd = tmp_path / "upload.exe"
    odd.write_bytes(b"x")
    assert failed.keep_file(odd, route="lan").audio.suffix == ".bin"  # never an .exe


def test_only_the_newest_few_are_kept(tmp_path):
    failed = FailedRecordings(tmp_path / "failed", max_items=3)
    ids = []
    for i in range(5):
        item = failed.keep_samples(np.full(1600, 0.1, np.float32), error=str(i))
        ids.append(item.id)
        time.sleep(0.002)
    kept = failed.list()
    assert len(kept) == 3
    assert [k.error for k in kept] == ["4", "3", "2"]  # newest first


def test_size_cap(tmp_path):
    one = 16_000 * 2 * 2 + 44  # 2 s of 16-bit mono WAV
    failed = FailedRecordings(tmp_path / "failed", max_items=10, max_bytes=int(one * 2.5))
    for i in range(4):
        failed.keep_samples(np.full(32_000, 0.1, np.float32), error=str(i))
        time.sleep(0.002)
    assert [k.error for k in failed.list()] == ["3", "2"]
    # a single recording bigger than the cap is still kept (never lose the newest)
    tiny = FailedRecordings(tmp_path / "tiny", max_bytes=10)
    assert tiny.keep_samples(np.full(32_000, 0.1, np.float32)) is not None
    assert len(tiny.list()) == 1


def test_remove_clear_and_bad_ids(tmp_path):
    failed = FailedRecordings(tmp_path / "failed")
    a = failed.keep_samples(np.full(1600, 0.1, np.float32))
    time.sleep(0.002)
    b = failed.keep_samples(np.full(1600, 0.1, np.float32))
    outside = tmp_path / "secret.json"
    outside.write_text("{}", "utf-8")
    for bad in ("../secret", "..\\secret", "*", "", "20261010-153000-zzzzzz"):
        assert failed.get(bad) is None
        assert failed.remove(bad) is False
    assert outside.exists()
    assert failed.remove(a.id) is True and failed.get(a.id) is None
    assert failed.clear() == 1 and failed.list() == []
    assert b.audio.exists() is False


def test_nothing_is_kept_while_history_is_off(tmp_path):
    failed = FailedRecordings(tmp_path / "failed", enabled=False)
    assert failed.keep_samples(np.full(1600, 0.1, np.float32)) is None
    src = tmp_path / "x.m4a"
    src.write_bytes(b"x")
    assert failed.keep_file(src, route="lan") is None
    assert not (tmp_path / "failed").exists()


def test_orphans_and_junk_are_ignored_or_tidied(tmp_path):
    folder = tmp_path / "failed"
    failed = FailedRecordings(folder)
    folder.mkdir()
    (folder / "20261010-153000-000001.wav").write_bytes(b"orphan audio")  # no .json
    (folder / "20261010-153001-000002.json").write_text("not json", "utf-8")
    (folder / "notes.txt").write_text("mine", "utf-8")
    assert failed.list() == []
    failed.keep_samples(np.full(1600, 0.1, np.float32))
    assert not (folder / "20261010-153000-000001.wav").exists()
    assert (folder / "notes.txt").exists()


# ---------------------------------------------------------------- pure copy


def test_when_labels():
    assert hm.when_label("2026-10-10T14:03:00-07:00", NOW) == "Today 14:03"
    assert hm.when_label("2026-10-09T09:05:00-07:00", NOW) == "Yesterday 9:05"
    assert hm.when_label("2026-10-03T14:03:00-07:00", NOW) == "3 Oct 14:03"
    assert hm.when_label("2025-12-24T10:00:00-07:00", NOW) == "24 Dec 2025"
    assert hm.when_label("garbage", NOW) == "" and hm.when_label(None, NOW) == ""


def test_source_labels():
    assert hm.source_label({"route": "local"}) == "This PC"
    assert hm.source_label({"route": "lan", "source": "Sam's iPhone"}) == "Sam's iPhone"
    assert hm.source_label({"route": "lan", "source": None}) == "Phone"
    assert hm.source_label({"route": "tunnel"}) == "Phone"
    assert hm.source_label({}) == "Phone"


def test_length_and_notes():
    assert hm.length_label(None) == "" and hm.length_label(0) == ""
    assert hm.length_label(42.4) == "42 s" and hm.length_label(125) == "2 min 05 s"
    assert hm.note_for({"recovered": True}) == "Written out later"
    assert hm.note_for({"target": "pc", "pasted": False, "warning": "locked"}) == "Not pasted"
    assert hm.note_for({"target": "phone", "pasted": False, "warning": None}) == ""
    assert hm.note_for({"target": "pc", "pasted": True}) == ""


def test_search_matches_every_word_in_any_case():
    assert hm.matches("Ship the Better Shell release", "This PC", "better RELEASE")
    assert hm.matches("anything", "Sam's iPhone", "sam")
    assert not hm.matches("Ship it", "This PC", "ship later")
    assert hm.matches("whatever", "x", "   ")


# ------------------------------------------------------------------- model


def model_with(tmp_path, records, *, history_on: bool = True, **kw):
    """The History window's model over ``records``. History is off by default on a fresh
    install, so these tests turn it on first (as the owner would) unless told otherwise."""
    copied: list[str] = []
    svc = make_services(tmp_path, copy_text=copied.append, **kw)
    if history_on:
        update_config(svc.paths, {"history": {"enabled": True}})
    write_history(svc.paths.history_file, records)
    model = hm.HistoryModel(svc)
    model.load()
    return model, svc, copied


def test_view_lists_newest_first_with_where_and_when(tmp_path):
    records = [
        rec(3, "Third oldest."),
        rec(2, "From the phone.", route="lan", source="Sam's iPhone"),
        rec(1, "", raw_text=""),  # nothing heard: not listed
        rec(0, "Newest, not pasted.", pasted=False, warning="locked"),
    ]
    model, _svc, _ = model_with(tmp_path, records)
    view = model.view(now=NOW)
    assert view.enabled and not view.loading and view.empty_text == ""
    assert [e.text for e in view.entries] == [
        "Newest, not pasted.",
        "From the phone.",
        "Third oldest.",
    ]
    assert [e.source for e in view.entries] == ["This PC", "Sam's iPhone", "This PC"]
    assert view.entries[0].when == "Today 15:30" and view.entries[0].note == "Not pasted"
    assert view.total == 3 and not view.limited


def test_view_search_and_limits(tmp_path):
    records = [rec(i, f"Note number {i}" + (" banana" if i % 2 else "")) for i in range(400)]
    model, _svc, _ = model_with(tmp_path, records)
    everything = model.view(now=NOW)
    assert len(everything.entries) == hm.SHOWN_LIMIT and everything.limited
    assert everything.entries[0].text.startswith("Note number 399")  # newest first
    bananas = model.view("BANANA", now=NOW)
    assert bananas.total == 200 and all("banana" in e.text for e in bananas.entries)
    none = model.view("kiwi", now=NOW)
    assert none.entries == [] and none.empty_text == hm.NO_MATCH_TEXT


def test_empty_history_says_so(tmp_path):
    model, _svc, _ = model_with(tmp_path, [])
    view = model.view()
    assert view.entries == [] and view.empty_text == hm.EMPTY_TEXT


def test_history_is_off_on_a_fresh_install_and_the_window_says_so(tmp_path):
    svc = make_services(tmp_path)  # a brand-new config dir, written from the template
    model = hm.HistoryModel(svc)
    assert model.enabled is False  # right from the first draw, before load()
    model.load()
    view = model.view()
    assert view.enabled is False and view.entries == []
    assert hm.OFF_TITLE == "History is off."
    # one line, and it says that recordings that fail are kept too once it's on
    assert hm.OFF_TEXT.startswith("Turn it on") and "recordings that fail" in hm.OFF_TEXT
    assert "this PC" in hm.OFF_TEXT and "this PC" in hm.SUBTITLE
    # and nothing that fails to go through is kept while it's off
    assert svc.failed_recordings().keep_samples(np.full(1600, 0.1, np.float32)) is None
    assert not svc.paths.failed_dir.exists()


def test_turn_on_from_the_window_saves_it_and_keeps_failed_recordings(tmp_path):
    svc = make_services(tmp_path)
    reloaded = []
    svc.on_settings_saved = lambda keys: reloaded.append(keys) or set()  # the hub applies it
    model = hm.HistoryModel(svc)
    model.load()
    assert model.turn_on() and model.message == "History is on."
    assert reloaded == [{"history.enabled"}]
    assert svc.history_enabled() is True
    model.load()
    assert model.view().enabled is True
    item = FailedRecordings(svc.paths.failed_dir, enabled=svc.history_enabled()).keep_samples(
        np.full(1600, 0.1, np.float32), error="boom"
    )
    assert item is not None


def test_history_off_is_said_plainly(tmp_path):
    copied: list[str] = []
    svc = make_services(tmp_path, copy_text=copied.append)
    svc.paths.config_file.write_text("[history]\nenabled = false\n", "utf-8")
    model = hm.HistoryModel(svc)
    model.load()
    assert model.view().enabled is False
    saved = []
    svc.save_settings = lambda changes: saved.append(changes) or set()
    assert model.turn_on() and saved == [{"history": {"enabled": True}}]
    assert model.message == "History is on." and model.enabled


def test_copy_puts_the_whole_text_on_the_clipboard(tmp_path):
    long = "A long dictation. " * 200
    model, _svc, copied = model_with(tmp_path, [rec(0, long.strip())])
    key = model.view().entries[0].key
    assert model.copy(key)
    assert copied == [" ".join(long.split())] and model.message == hm.COPIED
    assert not model.copy("nope") and model.error


def test_failed_recordings_are_listed_and_tried_again(tmp_path):
    model, svc, _ = model_with(tmp_path, [rec(0, "Older text.")])
    failed = svc.failed_recordings()
    item = failed.keep_samples(np.full(16_000 * 125, 0.1, np.float32), error="CUDA error")
    model.load()
    view = model.view(now=NOW)
    (entry,) = view.failed
    assert entry.id == item.id and entry.source == "This PC" and entry.length == "2 min 05 s"
    assert entry.error == "CUDA error" and view.can_retry is False
    # without a running hub there's no speech engine
    assert not model.retry(item.id) and model.message == hm.RETRY_NEEDS_HUB
    tried = []

    def retry(item_id):
        tried.append(item_id)
        return "Recovered words."

    svc.retry_failed = retry
    assert model.view().can_retry
    assert model.retry(item.id) and model.message == hm.RETRIED and tried == [item.id]
    svc.retry_failed = lambda _id: (_ for _ in ()).throw(EngineError("still broken"))
    assert not model.retry(item.id) and "still broken" in model.message and model.error
    svc.retry_failed = lambda _id: ""
    assert not model.retry(item.id) and "No words" in model.message
    assert model.delete_failed(item.id) and failed.list() == []
    assert not model.delete_failed(item.id)


def test_clear_history_deletes_records_and_kept_recordings(tmp_path):
    model, svc, _ = model_with(tmp_path, [rec(0, "Secret plans.")])
    svc.failed_recordings().keep_samples(np.full(1600, 0.1, np.float32))
    assert model.clear() and model.message == hm.CLEARED
    assert not svc.paths.history_file.exists() and svc.failed_recordings().list() == []
    model.load()
    assert model.view().entries == [] and model.view().failed == []


def test_clear_uses_the_hubs_history_log_lock(tmp_path):
    svc = make_services(tmp_path)
    calls = []
    svc.history_log = SimpleNamespace(path=svc.paths.history_file, clear=lambda: calls.append(1))
    svc.clear_history()
    assert calls == [1]


def test_clear_failure_is_reported(tmp_path):
    model, svc, _ = model_with(tmp_path, [rec(0, "x")])

    def broken():
        raise PermissionError("in use")

    svc.clear_history = broken
    assert not model.clear() and model.error


# ------------------------------------------------------------ hub & server


def test_hub_retry_transcribes_a_kept_recording_without_pasting(tmp_path):
    """hub.py's History > Try again, with the real Pipeline and fakes around it."""
    from fakes import FakeCleaner, FakeInjector, FakeVocabStore, fake_decode
    from shoutabout.config import Config
    from shoutabout.pipeline import Pipeline
    from shoutabout.state import HubState

    log = HistoryLog(tmp_path / "history.jsonl", tmp_path / "audio", False)
    injector = FakeInjector()
    pipeline = Pipeline(
        config=Config(),
        engine=FakeEngine(text="saved by the retry"),
        vocab=FakeVocabStore(),
        cleaner=FakeCleaner(),
        injector=injector,
        state=HubState(),
        history=log,
        decode=fake_decode,
    )
    failed = FailedRecordings(tmp_path / "failed")
    item = failed.keep_samples(np.full(16_000, 0.1, np.float32), error="boom")
    out = pipeline.process_file(
        item.audio,
        target="pc",
        route=item.route,
        deliver=False,
        extra={"source": item.source, "recovered": True},
    )
    assert out.text == "saved by the retry" and injector.pasted == []  # never pasted
    (record,) = read_history(log.path)
    assert record["recovered"] is True and record["route"] == "local"


def test_phone_dictations_record_the_phone_name(tmp_path):
    from test_server_v2 import HUB_TOKEN, Hub, bearer

    (tmp_path / "vocab.txt").write_text("x\n", "utf-8")
    hub = Hub(tmp_path)
    hub.ctx.pipeline.history = hub.history
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
    sources = [x.extra.get("source") for x in hub.history.records]
    assert sources == ["Sam's iPhone", None, "Sam's iPhone"]


def test_failed_upload_is_kept_only_when_the_engine_failed(tmp_path):
    from shoutabout.audio import AudioError
    from test_server_v2 import Hub, bearer

    (tmp_path / "vocab.txt").write_text("x\n", "utf-8")
    hub = Hub(tmp_path)
    hub.ctx.failed = failed = FailedRecordings(tmp_path / "failed")
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
    (item,) = failed.list()
    assert item.audio.read_bytes() == b"the phone's audio"
    assert (item.route, item.source) == ("lan", "Sam's iPhone")
    hub.ctx.pipeline.engine = FakeEngine(text="ok")

    def bad_audio(_path, _max=None):
        raise AudioError("not audio")

    hub.ctx.pipeline._decode = bad_audio
    assert upload().status_code == 415
    assert len(failed.list()) == 1  # bad audio: trying again wouldn't help


# --------------------------------------------------------------- the window


def test_ui_host_opens_one_history_window(tmp_path, monkeypatch):
    from shoutabout import history_ui, ui_host

    made = []

    class FakeHistoryWindow:
        def __init__(self, window, model, on_close=None) -> None:
            self.model, self.fronts, self.reloads = model, 0, 0
            made.append(self)

        def bring_to_front(self) -> None:
            self.fronts += 1

        def reload(self) -> None:
            self.reloads += 1

    monkeypatch.setattr(history_ui, "HistoryWindow", FakeHistoryWindow)
    host = ui_host.UiHost(make_services(tmp_path, with_pairing=False))
    monkeypatch.setattr(host, "_toplevel", lambda: object())
    host._handle("history", None)
    host._handle("history", None)
    assert len(made) == 1 and made[0].fronts == 2 and made[0].reloads == 1
    assert isinstance(made[0].model, hm.HistoryModel)


@pytest.mark.windows
def test_history_window_renders(tmp_path):
    tk = pytest.importorskip("tkinter")
    from shoutabout.history_ui import HistoryWindow

    root = tk.Tk()
    root.withdraw()
    try:
        model, svc, copied = model_with(
            tmp_path, [rec(i, f"Dictation {i} " + "words " * 30) for i in range(30)]
        )
        svc.failed_recordings().keep_samples(np.full(16_000 * 3, 0.1, np.float32), error="x")
        top = tk.Toplevel(root)
        top.withdraw()
        win = HistoryWindow(top, model, load_in_background=False)
        root.update()
        assert len(win._texts) == 30
        win.search.insert(0, "dictation 17")
        win._search_now()
        root.update()
        assert len(win._texts) == 1
        win._clear()
        root.update()
        assert win.clear_button["text"] == "Sure? Clear everything"
        win.close()
        assert copied == []
    finally:
        root.destroy()


@pytest.mark.windows
def test_history_window_when_off_says_so_and_turns_on(tmp_path):
    tk = pytest.importorskip("tkinter")
    from shoutabout.history_ui import HistoryWindow

    def texts(parent):
        out = []
        for child in parent.winfo_children():
            try:
                out.append(str(child.cget("text")))
            except Exception:
                pass
            out += texts(child)
        return out

    try:
        root = tk.Tk()
    except tk.TclError as exc:  # no display, or Tcl not ready for a second interpreter
        pytest.skip(f"no Tk: {exc}")
    root.withdraw()
    try:
        svc = make_services(tmp_path)  # a fresh install: history is off
        top = tk.Toplevel(root)
        top.withdraw()
        win = HistoryWindow(top, hm.HistoryModel(svc), load_in_background=False)
        root.update()
        shown = texts(win.body)
        assert hm.OFF_TITLE in shown and hm.OFF_TEXT in shown
        (button,) = [b for b in win.body.winfo_children() if b.cget("text") == "Turn history on"]
        button.invoke()
        end = time.monotonic() + 3
        while win._layout != "list" and time.monotonic() < end:
            root.update()
            time.sleep(0.02)
        assert win._layout == "list" and svc.history_enabled()
        assert win.model.message.startswith("History is on.")
        win.close()
    finally:
        root.destroy()
