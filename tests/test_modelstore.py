"""Model download: progress, cancel, resume, checksum and the cache layout (fake Hub)."""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import os
import threading
from pathlib import Path

import pytest

from shoutabout import modelstore
from shoutabout.modelstore import (
    DownloadCancelled,
    ModelStoreError,
    Plan,
    RemoteFile,
    Response,
)

COMMIT = "a" * 40
BIG = os.urandom(3 * 1024 * 1024 + 123)
SMALL = {"config.json": b'{"x": 1}', "tokenizer.json": b"{}", "vocabulary.json": b"[]"}


class FakeHub:
    """Serves one repo from memory with range requests; snapshot() mimics the Hub's
    layout (refs/main + snapshots/<commit>/...)."""

    def __init__(self, root: Path, *, ignore_range: bool = False, corrupt: bool = False) -> None:
        self.root = root
        self.ignore_range = ignore_range
        self.corrupt = corrupt
        self.opens: list[int] = []
        self.snapshots: list[bool] = []
        self.fail_plan: Exception | None = None
        self.chunk = 256 * 1024

    def plan(self, repo: str) -> Plan:
        if self.fail_plan is not None:
            raise self.fail_plan
        files = [RemoteFile("model.bin", len(BIG), hashlib.sha256(BIG).hexdigest())]
        files += [RemoteFile(name, len(data), None) for name, data in SMALL.items()]
        return Plan(COMMIT, tuple(files))

    def open(self, repo, plan, path, start):
        assert path == "model.bin"
        self.opens.append(start)
        data = BIG if not self.corrupt else b"x" * len(BIG)

        @contextlib.contextmanager
        def stream():
            if self.ignore_range or start == 0:
                status, body = 200, data
            elif start >= len(data):
                status, body = 416, b""
            else:
                status, body = 206, data[start:]
            chunks = (body[i : i + self.chunk] for i in range(0, len(body), self.chunk))
            yield Response(status, chunks)

        return stream()

    def folder(self, repo: str) -> Path:
        return modelstore.repo_folder(self.root, repo)

    def snapshot(self, repo, *, cache_dir, local_only):
        self.snapshots.append(local_only)
        folder = self.folder(repo)
        ref = folder / "refs" / "main"
        if local_only:
            if not ref.exists():
                raise FileNotFoundError("not cached")
            snap = folder / "snapshots" / ref.read_text()
            if not (snap / "model.bin").exists() or not (snap / "config.json").exists():
                raise FileNotFoundError("incomplete snapshot")
            return str(snap)
        snap = folder / "snapshots" / COMMIT
        snap.mkdir(parents=True, exist_ok=True)
        for name, data in SMALL.items():
            (snap / name).write_bytes(data)
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_text(COMMIT)
        return str(snap)

    def cache_root(self, cache_dir):
        return Path(cache_dir) if cache_dir else self.root


@pytest.fixture
def hub(tmp_path):
    fake = FakeHub(tmp_path / "cache")
    modelstore.set_backend(fake)
    yield fake
    modelstore.set_backend(None)


def total() -> int:
    return len(BIG) + sum(len(d) for d in SMALL.values())


def test_download_reports_progress_and_lands_in_the_cache_layout(hub):
    seen: list[tuple[int, int]] = []
    assert modelstore.is_ready("small.en") is False
    path = modelstore.download("small.en", progress=lambda d, t: seen.append((d, t)))
    assert path == hub.folder("Systran/faster-whisper-small.en") / "snapshots" / COMMIT
    assert (path / "model.bin").read_bytes() == BIG
    assert (path / "config.json").exists()
    assert modelstore.is_ready("small.en")
    assert modelstore.local_path("small.en") == path
    assert seen[0] == (0, total()) and seen[-1] == (total(), total())
    dones = [d for d, _ in seen]
    assert dones == sorted(dones) and len(seen) > 5  # bytes arrive chunk by chunk
    assert hub.opens == [0]
    assert not list(path.parent.parent.glob("blobs/*.partial"))


def test_ready_model_is_not_downloaded_again(hub):
    modelstore.download("small.en")
    hub.opens.clear()
    seen = []
    modelstore.download("small.en", progress=lambda d, t: seen.append((d, t)))
    assert hub.opens == []
    assert seen and seen[-1][0] == seen[-1][1]


def test_cancel_keeps_the_partial_file_and_resumes(hub):
    cancel = threading.Event()

    def progress(done, total_bytes):
        if done > len(BIG) // 3:
            cancel.set()

    with pytest.raises(DownloadCancelled):
        modelstore.download("small.en", progress=progress, cancel=cancel)
    blobs = hub.folder("Systran/faster-whisper-small.en") / "blobs"
    partial = next(blobs.glob("*.partial"))
    kept = partial.stat().st_size
    assert 0 < kept < len(BIG)
    assert modelstore.partial_bytes("small.en") == kept
    assert not modelstore.is_ready("small.en")

    seen = []
    path = modelstore.download("small.en", progress=lambda d, t: seen.append(d))
    assert hub.opens == [0, kept]  # the second request continued at the partial size
    assert (path / "model.bin").read_bytes() == BIG
    assert kept in seen  # progress picked up where it stopped
    assert modelstore.partial_bytes("small.en") == 0


def test_cancel_before_start(hub):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(DownloadCancelled):
        modelstore.download("small.en", cancel=cancel)
    assert hub.opens == []


def test_server_ignoring_the_range_starts_over(hub):
    cancel = threading.Event()
    with pytest.raises(DownloadCancelled):
        modelstore.download(
            "small.en", progress=lambda d, t: d > 1000 and cancel.set(), cancel=cancel
        )
    hub.ignore_range = True
    path = modelstore.download("small.en")
    assert (path / "model.bin").read_bytes() == BIG


def test_complete_partial_file_gets_416_and_is_used(hub):
    blobs = hub.folder("Systran/faster-whisper-small.en") / "blobs"
    blobs.mkdir(parents=True)
    sha = hashlib.sha256(BIG).hexdigest()
    (blobs / f"{sha}.partial").write_bytes(BIG)
    path = modelstore.download("small.en")
    assert hub.opens == [len(BIG)]
    assert (path / "model.bin").read_bytes() == BIG


def test_damaged_download_is_thrown_away(hub):
    hub.corrupt = True
    with pytest.raises(ModelStoreError, match="damaged"):
        modelstore.download("small.en")
    blobs = hub.folder("Systran/faster-whisper-small.en") / "blobs"
    assert not list(blobs.glob("*.partial"))


def test_offline_gives_a_plain_message(hub):
    class ConnectError(Exception):
        pass

    hub.fail_plan = ConnectError("no route")
    with pytest.raises(ModelStoreError, match="Check the internet connection"):
        modelstore.download("small.en")


def test_not_enough_disk_space(hub, monkeypatch):
    from collections import namedtuple

    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(modelstore.shutil, "disk_usage", lambda p: usage(1, 1, 1000))
    with pytest.raises(ModelStoreError, match="enough disk space"):
        modelstore.download("small.en")


def test_unknown_model(hub):
    with pytest.raises(ModelStoreError, match="Unknown speech model"):
        modelstore.download("huge-v9")
    assert modelstore.is_ready("huge-v9") is False


def test_local_folder_models(tmp_path, hub):
    folder = tmp_path / "my-model"
    folder.mkdir()
    assert modelstore.is_ready(str(folder)) is False
    (folder / "model.bin").write_bytes(b"x")
    (folder / "config.json").write_bytes(b"{}")
    assert modelstore.is_ready(str(folder))
    assert modelstore.download(str(folder)) == folder
    assert modelstore.repo_id(str(folder)) is None


def test_place_moves_the_blob_when_symlinks_are_not_allowed(tmp_path, monkeypatch):
    blob = tmp_path / "blobs" / "abc"
    blob.parent.mkdir()
    blob.write_bytes(b"data")
    pointer = tmp_path / "snapshots" / "c" / "model.bin"

    def no_symlinks(*a, **kw):
        raise OSError("A required privilege is not held by the client")

    monkeypatch.setattr(modelstore.os, "symlink", no_symlinks)
    modelstore._place(blob, pointer)
    assert pointer.read_bytes() == b"data" and not blob.exists()


def test_repo_ids_and_sizes():
    assert modelstore.repo_id("large-v3-turbo") == "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
    assert modelstore.repo_id("someone/custom-ct2") == "someone/custom-ct2"
    assert modelstore.repo_id("") is None
    assert modelstore.download_mb("large-v3-turbo") == 1622
    assert modelstore.download_mb("base.en") == 148
    assert modelstore.download_mb("someone/custom") == 0


class GatedHub(FakeHub):
    """Holds the big file's first chunk until the test opens the gate."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.gate = threading.Event()
        self.plans = 0

    def plan(self, repo):
        self.plans += 1
        return super().plan(repo)

    def open(self, repo, plan, path, start):
        inner = super().open(repo, plan, path, start)
        gate = self.gate

        @contextlib.contextmanager
        def stream():
            with inner as resp:
                first = True

                def chunks():
                    nonlocal first
                    for chunk in resp.chunks:
                        if first:
                            first = False
                            yield chunk
                            assert gate.wait(5)
                            continue
                        yield chunk

                yield Response(resp.status, chunks())

        return stream()


@pytest.fixture
def gated(tmp_path):
    fake = GatedHub(tmp_path / "cache")
    modelstore.set_backend(fake)
    yield fake
    fake.gate.set()
    modelstore.set_backend(None)


def run_in_thread(fn, **kw):
    out: dict = {}

    def target():
        try:
            out["path"] = fn(**kw)
        except BaseException as exc:
            out["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    return t, out


def wait_for(cond, timeout=5.0):
    import time

    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


def test_concurrent_callers_share_one_download(gated):
    first_seen, second_seen = [], []
    t1, out1 = run_in_thread(
        modelstore.download, stt_model="small.en", progress=lambda d, t: first_seen.append(d)
    )
    wait_for(lambda: first_seen and first_seen[-1] > 0)  # first chunk is in
    t2, out2 = run_in_thread(
        modelstore.download, stt_model="small.en", progress=lambda d, t: second_seen.append(d)
    )
    wait_for(lambda: bool(second_seen))  # the joiner hears where it is right away
    gated.gate.set()
    t1.join(5)
    t2.join(5)
    assert out1["path"] == out2["path"]
    assert gated.plans == 1 and gated.opens == [0]  # downloaded once
    assert second_seen[-1] == total()


def test_joiner_can_stop_waiting_without_stopping_the_download(gated):
    seen = []
    t1, out1 = run_in_thread(
        modelstore.download, stt_model="small.en", progress=lambda d, t: seen.append(d)
    )
    wait_for(lambda: seen and seen[-1] > 0)
    cancel = threading.Event()
    t2, out2 = run_in_thread(modelstore.download, stt_model="small.en", cancel=cancel)
    cancel.set()
    t2.join(5)
    assert isinstance(out2["error"], DownloadCancelled)
    gated.gate.set()
    t1.join(5)
    assert (out1["path"] / "model.bin").read_bytes() == BIG


def test_joiner_carries_on_when_the_first_caller_cancels(gated):
    seen = []
    cancel = threading.Event()
    t1, out1 = run_in_thread(
        modelstore.download,
        stt_model="small.en",
        progress=lambda d, t: seen.append(d),
        cancel=cancel,
    )
    wait_for(lambda: seen and seen[-1] > 0)
    t2, out2 = run_in_thread(modelstore.download, stt_model="small.en")
    cancel.set()
    gated.gate.set()
    t1.join(5)
    t2.join(5)
    assert isinstance(out1["error"], DownloadCancelled)
    assert (out2["path"] / "model.bin").read_bytes() == BIG
    assert gated.plans == 2 and gated.opens[0] == 0 and gated.opens[1] > 0  # resumed


def test_aliases_share_a_download_key():
    assert modelstore._job_key("turbo", None) == modelstore._job_key("large-v3-turbo", None)
    assert modelstore._job_key("small.en", "D:/a") != modelstore._job_key("small.en", "D:/b")


def test_tables_match_faster_whisper():
    """Our copies of faster-whisper's repo table and file patterns must not drift."""
    from faster_whisper import utils

    assert modelstore.MODEL_REPOS == utils._MODELS
    source = inspect.getsource(utils.download_model)
    for pattern in modelstore.ALLOW_PATTERNS:
        assert f'"{pattern}"' in source
    assert set(modelstore.KNOWN_DOWNLOAD_MB) <= set(modelstore.MODEL_REPOS)
