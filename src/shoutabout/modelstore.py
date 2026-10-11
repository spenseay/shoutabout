"""Whisper model files: is a model downloaded, and download it with progress.

The files live exactly where faster-whisper keeps them (the Hugging Face cache, or
``[stt] download_root``), in the cache layout ``faster_whisper.utils.download_model``
produces, so the engine finds them with ``local_files_only=True``. A model that is already
downloaded is never fetched again or moved.

:func:`download` reports progress in bytes, can be cancelled, and resumes. Current
huggingface_hub releases throw a partial file away when a download stops, which would make
an interrupted 1.6 GB first-run download start over. So the big file (``model.bin``) is
fetched here, in the documented cache layout (``blobs/<sha256>`` plus a pointer in
``snapshots/<commit>/``):

1. the Hub's tree listing gives the commit, every file's size and the big file's SHA-256;
2. ``model.bin`` streams into ``blobs/<sha256>.partial`` with an HTTP range request that
   continues where the last attempt stopped; every chunk updates the progress and checks
   ``cancel``; the SHA-256 is verified before the file is renamed into place and linked
   (or, without symlink rights, moved) into the snapshot folder;
3. ``huggingface_hub.snapshot_download`` then fetches the small files and writes
   ``refs/main`` and its tree cache exactly as faster-whisper's own download would.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

# What faster_whisper.utils.download_model fetches (keep in sync; a test checks it).
ALLOW_PATTERNS: tuple[str, ...] = (
    "config.json",
    "preprocessor_config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.*",
)
REQUIRED_FILES: tuple[str, ...] = ("model.bin", "config.json")

# faster_whisper.utils._MODELS (keep in sync; a test checks it). A copy, so asking "is it
# downloaded?" doesn't import faster-whisper, CTranslate2 and onnxruntime.
MODEL_REPOS: dict[str, str] = {
    "tiny.en": "Systran/faster-whisper-tiny.en",
    "tiny": "Systran/faster-whisper-tiny",
    "base.en": "Systran/faster-whisper-base.en",
    "base": "Systran/faster-whisper-base",
    "small.en": "Systran/faster-whisper-small.en",
    "small": "Systran/faster-whisper-small",
    "medium.en": "Systran/faster-whisper-medium.en",
    "medium": "Systran/faster-whisper-medium",
    "large-v1": "Systran/faster-whisper-large-v1",
    "large-v2": "Systran/faster-whisper-large-v2",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large": "Systran/faster-whisper-large-v3",
    "distil-large-v2": "Systran/faster-distil-whisper-large-v2",
    "distil-medium.en": "Systran/faster-distil-whisper-medium.en",
    "distil-small.en": "Systran/faster-distil-whisper-small.en",
    "distil-large-v3": "Systran/faster-distil-whisper-large-v3",
    "distil-large-v3.5": "distil-whisper/distil-large-v3.5-ct2",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}

# Download sizes (MB = 10^6 bytes) of the files above, from the Hub listing (2026-10).
# Only used to tell people how big a download is before it starts.
KNOWN_DOWNLOAD_MB: dict[str, int] = {
    "tiny.en": 78,
    "base.en": 148,
    "small.en": 486,
    "distil-small.en": 336,
    "medium.en": 1531,
    "distil-medium.en": 792,
    "distil-large-v3": 1517,
    "large-v3-turbo": 1622,
    "turbo": 1622,
}

CHUNK_BYTES = 1024 * 1024
DISK_MARGIN_BYTES = 200 * 1024**2
PARTIAL_SUFFIX = ".partial"
HTTP_TIMEOUT_S = 30.0

Progress = Callable[[int, int], None]


class ModelStoreError(RuntimeError):
    """A download failed; the message is plain words for the user."""


class DownloadCancelled(ModelStoreError):
    """The download was cancelled. The partial file is kept and resumed next time."""

    def __init__(self, message: str = "The download was cancelled.") -> None:
        super().__init__(message)


@dataclass(frozen=True)
class RemoteFile:
    path: str  # file name inside the repo
    size: int
    sha256: str | None = None  # LFS files: content hash = blob name in the cache


@dataclass(frozen=True)
class Plan:
    commit: str
    files: tuple[RemoteFile, ...]

    @property
    def total(self) -> int:
        return sum(f.size for f in self.files)


@dataclass
class Response:
    """One streaming GET: the status (200, 206 or 416), and the body chunks."""

    status: int
    chunks: Iterator[bytes]


# ---------------------------------------------------------------------------- naming


def repo_id(stt_model: str) -> str | None:
    """The Hugging Face repo of a model name or repo id; None for a local folder."""
    name = stt_model.strip()
    if not name or os.path.isdir(name):
        return None
    if "/" in name:
        return name
    return MODEL_REPOS.get(name)


def download_mb(stt_model: str) -> int:
    """Approximate download size in MB (0 when unknown or a local folder)."""
    return KNOWN_DOWNLOAD_MB.get(stt_model.strip(), 0)


def _has_required(folder: str | os.PathLike[str]) -> bool:
    return all(os.path.isfile(os.path.join(folder, f)) for f in REQUIRED_FILES)


# --------------------------------------------------------------------------- backend


class Backend(Protocol):
    def plan(self, repo: str) -> Plan: ...
    def open(
        self, repo: str, plan: Plan, path: str, start: int
    ) -> AbstractContextManager[Response]: ...
    def snapshot(self, repo: str, *, cache_dir: str | None, local_only: bool) -> str: ...
    def cache_root(self, cache_dir: str | None) -> Path: ...


class HubBackend:
    """The real Hugging Face calls (imported lazily)."""

    def plan(self, repo: str) -> Plan:
        from huggingface_hub import HfApi
        from huggingface_hub.hf_api import RepoFile
        from huggingface_hub.utils import filter_repo_objects

        api = HfApi()
        commit = api.repo_info(repo).sha
        if not commit:
            raise ModelStoreError("The model repository didn't report a revision.")
        entries = {
            e.path: e
            for e in api.list_repo_tree(repo, recursive=True, revision=commit)
            if isinstance(e, RepoFile)
        }
        files = []
        for path in filter_repo_objects(items=list(entries), allow_patterns=list(ALLOW_PATTERNS)):
            e = entries[path]
            sha = e.lfs.sha256 if e.lfs is not None else None
            files.append(RemoteFile(path, int(e.size or 0), sha))
        return Plan(commit, tuple(files))

    def open(
        self, repo: str, plan: Plan, path: str, start: int
    ) -> AbstractContextManager[Response]:
        import contextlib

        import httpx
        from huggingface_hub import hf_hub_url

        from shoutabout import __version__

        url = hf_hub_url(repo, path, revision=plan.commit)
        headers = {"User-Agent": f"shoutabout/{__version__}"}
        if start > 0:
            headers["Range"] = f"bytes={start}-"

        @contextlib.contextmanager
        def stream() -> Iterator[Response]:
            timeout = httpx.Timeout(HTTP_TIMEOUT_S, connect=15.0)
            with (
                httpx.Client(follow_redirects=True, timeout=timeout) as client,
                client.stream("GET", url, headers=headers) as resp,
            ):
                if resp.status_code not in (200, 206, 416):
                    resp.raise_for_status()
                yield Response(resp.status_code, resp.iter_bytes(CHUNK_BYTES))

        return stream()

    def snapshot(self, repo: str, *, cache_dir: str | None, local_only: bool) -> str:
        from huggingface_hub import snapshot_download

        return str(
            snapshot_download(
                repo,
                allow_patterns=list(ALLOW_PATTERNS),
                local_files_only=local_only,
                cache_dir=cache_dir,
                tqdm_class=_quiet_bar_class(),
            )
        )

    def cache_root(self, cache_dir: str | None) -> Path:
        if cache_dir:
            return Path(cache_dir)
        from huggingface_hub import constants

        return Path(constants.HF_HUB_CACHE)


def _quiet_bar_class() -> type:
    """faster-whisper silences Hugging Face's progress bars too (no console under pythonw)."""
    from tqdm import tqdm

    class QuietBar(tqdm):  # type: ignore[misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["disable"] = True
            kwargs.pop("name", None)
            super().__init__(*args, **kwargs)

    return QuietBar


_backend: Backend = HubBackend()


def set_backend(backend: Backend | None) -> None:
    """Swap the Hub backend (tests). None restores the real one."""
    global _backend
    _backend = backend if backend is not None else HubBackend()


# ---------------------------------------------------------------------------- status


def local_path(stt_model: str, *, cache_dir: str | None = None) -> Path | None:
    """The model's folder when it is fully downloaded (or a valid local folder), else None.
    Never touches the network."""
    name = stt_model.strip()
    if name and os.path.isdir(name):
        return Path(name) if _has_required(name) else None
    repo = repo_id(name)
    if repo is None:
        return None
    try:
        folder = _backend.snapshot(repo, cache_dir=cache_dir, local_only=True)
    except Exception as exc:  # LocalEntryNotFoundError, incomplete snapshot, bad cache...
        log.debug("model %s not in the local cache: %s", name, type(exc).__name__)
        return None
    return Path(folder) if _has_required(folder) else None


def is_ready(stt_model: str, *, cache_dir: str | None = None) -> bool:
    """True when the model can load without a download."""
    return local_path(stt_model, cache_dir=cache_dir) is not None


# -------------------------------------------------------------------------- download


def repo_folder(root: Path, repo: str) -> Path:
    """``models--org--name`` inside the cache root (the documented cache layout)."""
    return root / ("models--" + repo.replace("/", "--"))


class _Meter:
    """Bytes done so far, reported to the caller's progress callback."""

    def __init__(self, total: int, progress: Progress | None) -> None:
        self.total = total
        self.done = 0
        self._progress = progress

    def add(self, n: int) -> None:
        self.done += n
        self.report()

    def report(self) -> None:
        if self._progress is None:
            return
        try:
            self._progress(min(self.done, self.total), self.total)
        except Exception:
            log.exception("download progress callback failed")


def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise DownloadCancelled()


def _hash_file(path: Path, cancel: threading.Event | None) -> Any:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(8 * CHUNK_BYTES):
            _check_cancel(cancel)
            digest.update(chunk)
    return digest


def _fetch_blob(
    repo: str,
    plan: Plan,
    f: RemoteFile,
    blob: Path,
    meter: _Meter,
    cancel: threading.Event | None,
) -> None:
    """Download one LFS file into ``blob``, resuming ``blob.partial``; SHA-256 checked."""
    part = blob.with_name(blob.name + PARTIAL_SUFFIX)
    have = part.stat().st_size if part.exists() else 0
    if have > f.size:  # not ours to resume
        part.unlink()
        have = 0
    digest = _hash_file(part, cancel) if have else hashlib.sha256()
    meter.add(have)
    if have:
        log.info("resuming %s at %.0f of %.0f MB", f.path, have / 1e6, f.size / 1e6)
    with _backend.open(repo, plan, f.path, have) as resp:
        if resp.status == 416:  # nothing left to send: the partial file is complete
            pass
        else:
            mode = "ab"
            if resp.status == 200 and have:  # the server ignored the range: start over
                meter.add(-have)
                digest, have, mode = hashlib.sha256(), 0, "wb"
            with part.open(mode) as out:
                for chunk in resp.chunks:
                    _check_cancel(cancel)
                    out.write(chunk)
                    digest.update(chunk)
                    have += len(chunk)
                    meter.add(len(chunk))
    if have != f.size or (f.sha256 and digest.hexdigest() != f.sha256):
        part.unlink(missing_ok=True)
        raise ModelStoreError(
            "The speech model download was damaged on the way. It will start over next time."
        )
    os.replace(part, blob)


def _place(blob: Path, pointer: Path) -> None:
    """Make ``pointer`` (snapshots/<commit>/<file>) refer to ``blob`` the way
    huggingface_hub does: a relative symlink, or the file itself when symlinks aren't
    allowed (Windows without Developer Mode)."""
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.unlink(missing_ok=True)
    try:
        from huggingface_hub import constants

        no_links = bool(constants.HF_HUB_DISABLE_SYMLINKS)
    except ImportError:
        no_links = False
    if not no_links:
        try:
            os.symlink(os.path.relpath(blob, pointer.parent), pointer)
            return
        except (OSError, NotImplementedError):
            pass
    os.replace(blob, pointer)


def _friendly(exc: BaseException, need_mb: int) -> str:
    if isinstance(exc, ModelStoreError):
        return str(exc)
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:  # ENOSPC
        return f"There isn't enough disk space for the speech model (it needs about {need_mb} MB)."
    name = type(exc).__name__
    if any(k in name for k in ("Connect", "Timeout", "Network", "Offline", "Protocol", "HTTP")):
        return "Couldn't download the speech model. Check the internet connection and try again."
    return f"Couldn't download the speech model ({name}: {exc})."


def _check_space(root: Path, need: int, total: int) -> None:
    probe = root
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return
    if free < need + DISK_MARGIN_BYTES:
        raise ModelStoreError(
            f"There isn't enough disk space for the speech model: it needs about "
            f"{total / 1e6:,.0f} MB and {free / 1e6:,.0f} MB is free."
        )


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


class _Job:
    """One download in flight. Later callers for the same model join it: they wait for
    the same result and hear the same progress."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.path: Path | None = None
        self.error: BaseException | None = None
        self._lock = threading.Lock()
        self._last: tuple[int, int] | None = None
        self._listeners: list[Progress] = []

    def add(self, listener: Progress | None) -> None:
        if listener is None:
            return
        with self._lock:
            self._listeners.append(listener)
            last = self._last
        if last is not None:  # a joiner sees where the download is right away
            _call(listener, *last)

    def remove(self, listener: Progress | None) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def report(self, done: int, total: int) -> None:
        with self._lock:
            self._last = (done, total)
            listeners = list(self._listeners)
        for listener in listeners:
            _call(listener, done, total)


def _call(listener: Progress, done: int, total: int) -> None:
    try:
        listener(done, total)
    except Exception:
        log.exception("download progress callback failed")


_jobs: dict[tuple[str, str], _Job] = {}
_jobs_lock = threading.Lock()
JOIN_POLL_S = 0.1


def _job_key(name: str, cache_dir: str | None) -> tuple[str, str]:
    """Same repo + same cache = same download ("turbo" and "large-v3-turbo" included)."""
    repo = repo_id(name) or os.path.normcase(os.path.abspath(name))
    root = os.path.normcase(os.path.abspath(cache_dir)) if cache_dir else ""
    return repo.lower(), root


def download(
    stt_model: str,
    *,
    progress: Progress | None = None,
    cancel: threading.Event | None = None,
    cache_dir: str | None = None,
) -> Path:
    """Download a model into the cache (no-op if it's there) and return its folder.

    ``progress(done_bytes, total_bytes)`` is called as bytes arrive; ``cancel`` stops
    waiting (raises :class:`DownloadCancelled`; a partial file is kept and resumed next
    time). Raises :class:`ModelStoreError` with a plain-words message.

    Safe to call from several threads at once (the setup window and the hub's loader may
    both ask): the first caller downloads, later callers for the same model join it, get
    the same progress and the same folder. A joiner that cancels only stops waiting; if
    the first caller cancels, a joiner carries on (resuming) by itself.
    """
    name = stt_model.strip()
    key = _job_key(name, cache_dir)
    while True:
        with _jobs_lock:
            job = _jobs.get(key)
            owner = job is None
            if job is None:
                job = _jobs[key] = _Job()
        job.add(progress)
        if owner:
            try:
                job.path = _download_now(
                    name, progress=job.report, cancel=cancel, cache_dir=cache_dir
                )
                return job.path
            except BaseException as exc:
                job.error = exc
                raise
            finally:
                with _jobs_lock:
                    _jobs.pop(key, None)
                job.remove(progress)
                job.done.set()
        log.info("speech model %s is already downloading; waiting for it", name)
        try:
            while not job.done.wait(JOIN_POLL_S):
                _check_cancel(cancel)
        finally:
            job.remove(progress)
        if job.path is not None:
            return job.path
        error = job.error
        if isinstance(error, DownloadCancelled):
            _check_cancel(cancel)
            continue  # the first caller gave up; carry on ourselves
        if isinstance(error, ModelStoreError):
            raise ModelStoreError(str(error)) from error
        raise ModelStoreError(_friendly(error or RuntimeError("?"), download_mb(name))) from error


def _download_now(
    name: str,
    *,
    progress: Progress | None,
    cancel: threading.Event | None,
    cache_dir: str | None,
) -> Path:
    ready = local_path(name, cache_dir=cache_dir)
    if ready is not None:
        size = sum(_file_size(ready / f) for f in os.listdir(ready))
        _Meter(size, progress).add(size)
        return ready
    repo = repo_id(name)
    if repo is None:
        raise ModelStoreError(f"Unknown speech model {name!r}.")
    _check_cancel(cancel)
    try:
        plan = _backend.plan(repo)
    except Exception as exc:
        raise ModelStoreError(_friendly(exc, download_mb(name))) from exc
    root = _backend.cache_root(cache_dir)
    folder = repo_folder(root, repo)
    blobs, snapshot = folder / "blobs", folder / "snapshots" / plan.commit
    meter = _Meter(plan.total, progress)

    big = [f for f in plan.files if f.sha256]
    pending = [f for f in big if _file_size(snapshot / f.path) != f.size]
    need = sum(f.size for f in pending) - sum(
        _file_size(blobs / (f.sha256 + PARTIAL_SUFFIX)) for f in pending if f.sha256
    )
    _check_space(root, need, plan.total)
    log.info("downloading speech model %s (%.0f MB) into %s", name, plan.total / 1e6, root)
    meter.report()
    try:
        blobs.mkdir(parents=True, exist_ok=True)
        for f in big:
            if f not in pending:
                meter.add(f.size)
                continue
            assert f.sha256 is not None
            blob = blobs / f.sha256
            if _file_size(blob) == f.size:
                meter.add(f.size)
            else:
                _fetch_blob(repo, plan, f, blob, meter, cancel)
            _place(blob, snapshot / f.path)
        _check_cancel(cancel)
        path = Path(_backend.snapshot(repo, cache_dir=cache_dir, local_only=False))
    except DownloadCancelled:
        log.info("speech model download cancelled; it resumes next time")
        raise
    except Exception as exc:
        raise ModelStoreError(_friendly(exc, download_mb(name))) from exc
    if not _has_required(path):
        raise ModelStoreError("The speech model download is incomplete. Try again.")
    meter.done = meter.total
    meter.report()
    log.info("speech model %s ready in %s", name, path)
    return path


def partial_bytes(stt_model: str, *, cache_dir: str | None = None) -> int:
    """Bytes of an interrupted download waiting to be resumed (0 if none)."""
    repo = repo_id(stt_model)
    if repo is None:
        return 0
    try:
        blobs = repo_folder(_backend.cache_root(cache_dir), repo) / "blobs"
        return sum(_file_size(p) for p in blobs.glob("*" + PARTIAL_SUFFIX))
    except OSError:
        return 0
