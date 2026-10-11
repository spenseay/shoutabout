from __future__ import annotations

import json
import re
import threading
from datetime import datetime

from shoutabout.history import RECORD_KEYS, HistoryLog
from shoutabout.models import CleanupResult, Timings, TranscribeResult


def result(**kw) -> TranscribeResult:
    data = dict(
        text="Hello, Better Shell — ünïcödé ✓",
        raw_text="hello better shell",
        target="pc",
        route="lan",
        pasted=True,
        warning=None,
        cleanup=CleanupResult(text="x", status="applied", reason=None, duration_ms=5),
        timings=Timings(decode=1.4, stt=200.6, cleanup=50, paste=3, total=260),
        audio_seconds=4.234,
        request_id="abcdef123456",
        extra={"stt_model": "faster-whisper:large-v3-turbo@cuda/float16", "llm_model": "qwen"},
    )
    data.update(kw)
    return TranscribeResult(**data)


def read(path):
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


def test_schema(tmp_path):
    h = HistoryLog(tmp_path / "h.jsonl", tmp_path / "audio", keep_audio=False)
    h.append(result())
    (rec,) = read(tmp_path / "h.jsonl")
    assert tuple(rec) == RECORD_KEYS
    assert set(rec) == {
        "ts",
        "id",
        "route",
        "target",
        "raw_text",
        "text",
        "cleanup",
        "cleanup_fallback",
        "cleanup_reason",
        "pasted",
        "warning",
        "audio_seconds",
        "audio_file",
        "stt_model",
        "llm_model",
        "timings_ms",
        "source",
    }
    assert rec["source"] is None and "recovered" not in rec
    assert rec["id"] == "abcdef123456"
    assert rec["route"] == "lan"
    assert rec["target"] == "pc"
    assert rec["text"] == "Hello, Better Shell — ünïcödé ✓"
    assert rec["raw_text"] == "hello better shell"
    assert rec["cleanup"] == "applied"
    assert rec["cleanup_fallback"] is False
    assert rec["cleanup_reason"] is None
    assert rec["pasted"] is True
    assert rec["warning"] is None
    assert rec["audio_seconds"] == 4.23
    assert rec["audio_file"] is None
    assert rec["stt_model"].startswith("faster-whisper")
    assert rec["llm_model"] == "qwen"
    assert rec["timings_ms"] == {"decode": 1, "stt": 201, "cleanup": 50, "paste": 3, "total": 260}
    ts = datetime.fromisoformat(rec["ts"])
    assert ts.tzinfo is not None  # local time with offset


def test_non_ascii_written_raw_and_one_line_each(tmp_path):
    h = HistoryLog(tmp_path / "h.jsonl", tmp_path / "audio", keep_audio=False)
    h.append(result())
    h.append(result(cleanup=CleanupResult("x", "fallback", reason="guard: similarity 0.3")))
    raw = (tmp_path / "h.jsonl").read_bytes()
    assert "ünïcödé ✓".encode() in raw
    assert raw.count(b"\n") == 2
    assert b"\r" not in raw
    second = read(tmp_path / "h.jsonl")[1]
    assert second["cleanup"] == "fallback"
    assert second["cleanup_fallback"] is True
    assert second["cleanup_reason"] == "guard: similarity 0.3"


def test_creates_parent_dir(tmp_path):
    path = tmp_path / "deep" / "dir" / "h.jsonl"
    HistoryLog(path, tmp_path / "audio", keep_audio=False).append(result())
    assert len(read(path)) == 1


def test_keep_audio_copies_file(tmp_path):
    src = tmp_path / "upload.wav"
    src.write_bytes(b"RIFF....")
    h = HistoryLog(tmp_path / "h.jsonl", tmp_path / "audio", keep_audio=True)
    h.append(result(), audio_path=src)
    (rec,) = read(tmp_path / "h.jsonl")
    assert re.fullmatch(r"\d{8}-\d{6}-abcdef123456\.wav", rec["audio_file"])
    assert (tmp_path / "audio" / rec["audio_file"]).read_bytes() == b"RIFF...."
    assert src.exists()  # copied, not moved


def test_keep_audio_default_suffix(tmp_path):
    src = tmp_path / "noext"
    src.write_bytes(b"data")
    h = HistoryLog(tmp_path / "h.jsonl", tmp_path / "audio", keep_audio=True)
    h.append(result(), audio_path=src)
    assert read(tmp_path / "h.jsonl")[0]["audio_file"].endswith(".m4a")


def test_keep_audio_false_does_not_copy(tmp_path):
    src = tmp_path / "upload.m4a"
    src.write_bytes(b"data")
    HistoryLog(tmp_path / "h.jsonl", tmp_path / "audio", keep_audio=False).append(
        result(), audio_path=src
    )
    assert not (tmp_path / "audio").exists()


def test_missing_audio_still_logs_record(tmp_path):
    h = HistoryLog(tmp_path / "h.jsonl", tmp_path / "audio", keep_audio=True)
    h.append(result(), audio_path=tmp_path / "gone.m4a")
    assert read(tmp_path / "h.jsonl")[0]["audio_file"] is None


def test_never_raises(tmp_path, caplog):
    bad = tmp_path / "is_a_dir"
    bad.mkdir()
    HistoryLog(bad, tmp_path / "audio", keep_audio=False).append(result())
    assert "history" in caplog.text


def test_thread_safe(tmp_path):
    h = HistoryLog(tmp_path / "h.jsonl", tmp_path / "audio", keep_audio=False)

    def worker(i):
        for j in range(25):
            h.append(result(request_id=f"{i:02d}{j:010d}", text="x" * 2000))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    records = read(tmp_path / "h.jsonl")
    assert len(records) == 200
    assert len({r["id"] for r in records}) == 200
