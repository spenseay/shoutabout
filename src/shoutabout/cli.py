"""Command line: ``python -m shoutabout <command>``.

Imports are lazy so quick commands (``info``, ``token``) start fast and work on any OS.
Exit codes: 0 ok, 1 failure, 2 usage error.
"""

from __future__ import annotations

import argparse
import difflib
import glob
import json
import os
import re
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from shoutabout import __version__
from shoutabout.config import Config, ConfigError, Paths, default_home

AUDIO_EXTENSIONS = (".m4a", ".wav", ".mp3", ".caf", ".aac", ".flac", ".ogg")
MANIFEST_NAME = "manifest.json"

INFO_KEYS = (
    "home",
    "config_file",
    "vocab_file",
    "token_file",
    "log_dir",
    "history_file",
    "port",
    "host",
    "lan_ip",
    "lan_url",
    "stt_model",
    "stt_device",
    "stt_tier",
    "cleanup_enabled",
    "cleanup_model",
    "ollama_url",
    "access_enabled",
    "public_hostname",
)


class CliError(Exception):
    """A user-facing failure (printed as ``error: ...``, exit code 1)."""


# ------------------------------------------------------------------------ helpers


def _paths(args: argparse.Namespace) -> Paths:
    home = getattr(args, "home", None)
    return Paths(Path(home).expanduser()) if home else Paths(default_home())


def _load_config(paths: Paths) -> Config:
    from shoutabout.config import load_config

    return load_config(paths)


def read_install_state(paths: Paths) -> dict[str, Any]:
    """install-state.json written by the setup scripts; {} if missing or unreadable."""
    try:
        data = json.loads(paths.install_state_file.read_text("utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _err(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)


def _safe_streams() -> None:
    # Never crash on characters the console code page can't show (em dashes, QR blocks).
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except Exception:
                pass


def _can_encode(text: str) -> bool:
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def print_qr(text: str, *, out: Any = None) -> None:
    """Render ``text`` as a QR code in the terminal (compact Unicode blocks if possible)."""
    import segno

    qr = segno.make(text, error="m", micro=False)
    out = out or sys.stdout
    if _can_encode("▀▄█"):
        qr.terminal(out=out, compact=True)
    else:
        qr.terminal(compact=False)


# ---------------------------------------------------------- transcribe helpers


def normalize_words(text: str) -> list[str]:
    """Lowercase, drop apostrophes, other punctuation -> space, split into words."""
    text = text.lower().replace("'", "").replace("’", "")
    return re.sub(r"[^\w]+", " ", text).split()


def word_accuracy(expected: str, actual: str) -> float:
    """difflib word-level similarity ratio (0..1) of the normalised word lists."""
    exp, act = normalize_words(expected), normalize_words(actual)
    if not exp and not act:
        return 1.0
    return difflib.SequenceMatcher(None, exp, act, autojunk=False).ratio()


def term_hits(text: str, terms: Sequence[str]) -> dict[str, bool]:
    """Which expected vocab terms appear verbatim (case-sensitive) in ``text``."""
    return {term: term in text for term in terms}


def _has_glob(item: str) -> bool:
    return any(ch in item for ch in "*?[")


def expand_inputs(items: Sequence[str]) -> tuple[list[Path], list[str]]:
    """Files, directories (audio files inside, sorted) and glob patterns -> file list.

    Returns (files, problems). Windows shells don't expand wildcards, so we do.
    """
    files: list[Path] = []
    problems: list[str] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        key = path.resolve()
        if key not in seen:
            seen.add(key)
            files.append(path)

    for item in items:
        path = Path(item)
        if path.is_dir():
            found = sorted(
                (p for p in path.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS),
                key=lambda p: p.name.lower(),
            )
            if not found:
                problems.append(f"no audio files in {path}")
            for p in found:
                add(p)
        elif path.is_file():
            add(path)
        elif _has_glob(item):
            matches = sorted(glob.glob(item), key=str.lower)
            matched = [Path(m) for m in matches if Path(m).is_file()]
            if not matched:
                problems.append(f"no files match {item}")
            for p in matched:
                add(p)
        else:
            problems.append(f"not found: {item}")
    return files, problems


def load_manifest(directory: Path) -> dict[str, dict[str, Any]]:
    """``<dir>/manifest.json`` entries keyed by lowercased file name ({} if absent)."""
    path = directory / MANIFEST_NAME
    try:
        data = json.loads(path.read_text("utf-8-sig"))
    except (OSError, ValueError):
        return {}
    entries: dict[str, dict[str, Any]] = {}
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict) and isinstance(entry.get("file"), str):
                entries[Path(entry["file"]).name.lower()] = entry
    return entries


def evaluate_against_manifest(entry: dict[str, Any], raw_text: str, text: str) -> dict[str, Any]:
    expected = str(entry.get("text") or "")
    terms = [t for t in entry.get("expect_terms") or [] if isinstance(t, str)]
    return {
        "expected": expected,
        "kind": entry.get("kind"),
        "word_accuracy_raw": round(word_accuracy(expected, raw_text), 4),
        "word_accuracy": round(word_accuracy(expected, text), 4),
        "terms_raw": term_hits(raw_text, terms),
        "terms": term_hits(text, terms),
    }


# ------------------------------------------------------------------- commands


def cmd_run(args: argparse.Namespace) -> int:
    from shoutabout.hub import run_hub

    return run_hub(_paths(args), tray=not args.no_tray, greet=args.greet)


def cmd_init(args: argparse.Namespace) -> int:
    from shoutabout.config import ensure_initialized

    paths = _paths(args)
    report = ensure_initialized(paths, rotate_token=args.rotate_token)
    print(f"ShoutAbout config dir: {paths.home}")
    for path in report.created:
        print(f"  created  {path}")
    for path in report.existing:
        print(f"  exists   {path}")
    if args.port is not None:
        from shoutabout.config import set_server_port

        if set_server_port(paths, args.port):
            print(f"  set      [server] port = {args.port}")
    if args.rotate_token and paths.token_file in report.created:
        print("Hub token rotated: update the token in the iPhone Shortcut.")
    print("Next: `python -m shoutabout token --copy` (or `token --qr`) for the phone.")
    return 0


def collect_info(paths: Paths) -> dict[str, Any]:
    from shoutabout import netinfo
    from shoutabout.modelpick import resolve

    cfg = _load_config(paths)
    state = read_install_state(paths)
    choice = resolve(cfg)  # "auto" values -> what this PC runs
    return {
        "home": str(paths.home),
        "config_file": str(paths.config_file),
        "vocab_file": str(paths.vocab_file),
        "token_file": str(paths.token_file),
        "log_dir": str(paths.log_dir),
        "history_file": str(paths.history_file),
        "port": cfg.server.port,
        "host": cfg.server.host,
        "lan_ip": netinfo.lan_ipv4(),
        "lan_url": netinfo.lan_url(cfg),
        "stt_model": choice.stt_model,
        "stt_device": choice.device,
        "stt_tier": choice.tier,
        "cleanup_enabled": choice.cleanup,
        "cleanup_model": choice.cleanup_model or cfg.cleanup.model,
        "ollama_url": cfg.cleanup.ollama_url,
        "access_enabled": cfg.access.enabled,
        "public_hostname": state.get("public_hostname") or None,
    }


def cmd_info(args: argparse.Namespace) -> int:
    info = collect_info(_paths(args))
    if args.json:
        print(json.dumps(info, ensure_ascii=False))
        return 0
    width = max(len(k) for k in info)
    for key, value in info.items():
        shown = "-" if value is None else value
        print(f"{key.ljust(width)}  {shown}")
    return 0


def cmd_token(args: argparse.Namespace) -> int:
    from shoutabout.config import ensure_initialized, load_token

    paths = _paths(args)
    ensure_initialized(paths)
    token = load_token(paths)
    if args.copy:
        from shoutabout.inject import clipboard

        clipboard.set_text(token, exclude_from_history=True)
        print("Hub token copied to the clipboard (excluded from clipboard history).")
        return 0
    if args.qr:
        print_qr(token)
        print("Scan with the iPhone camera, then paste into the Shortcut.")
        return 0
    print(token)
    return 0


def cmd_qr(args: argparse.Namespace) -> int:
    if args.clipboard:
        from shoutabout.inject import clipboard

        text = clipboard.get_text()
        if not text:
            raise CliError("the clipboard has no text")
    elif args.text:
        text = args.text
    else:
        raise CliError("give TEXT or --clipboard")
    if args.open:
        return _open_qr_svg(text)
    print_qr(text)
    return 0


def _open_qr_svg(text: str) -> int:
    import segno

    qr = segno.make(text, error="m", micro=False)
    fd, name = tempfile.mkstemp(prefix="shoutabout-qr-", suffix=".svg")
    os.close(fd)
    path = Path(name)
    try:
        qr.save(str(path), kind="svg", scale=10, border=4)
        if hasattr(os, "startfile"):
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            print(f"QR code written to {path}")
        try:
            input("Scan it with the phone, then press Enter to delete the QR file... ")
        except EOFError:
            pass
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            print(f"warning: couldn't delete {path}; delete it manually", file=sys.stderr)
    return 0


SIGNING_NOTE = (
    "Signing: the shortcut structure (actions, the URLs and the Wi-Fi name; no tokens, no audio, "
    "no text) is sent to RoutineHub's HubSign service to get an Apple-signed file. "
    "--no-sign writes unsigned .plist files instead, for signing on a Mac "
    "(shortcuts sign --mode anyone)."
)


def _public_transcribe_url(paths: Paths) -> str | None:
    host = read_install_state(paths).get("public_hostname")
    if not isinstance(host, str) or not host.strip():
        return None
    host = host.strip()
    if "://" not in host:
        host = "https://" + host
    return host.rstrip("/") + "/transcribe"


CHERRI_NOTE = (
    "Cherri source: nothing was signed or sent anywhere. Compile and sign each .cherri file "
    "with the official Cherri playground (https://playground.cherrilang.org) or the Cherri "
    "compiler (cherri <file>.cherri). They compile with Cherri 2.3.0 but haven't been run on "
    "an iPhone yet. Dictate and Teach ask for the pairing text when added: run "
    "python -m shoutabout pair --cloudflare and copy the QR code's text (docs/ios-shortcuts.md, "
    "Signing)."
)

# make-shortcuts --which keys, in output order (shortcuts.SHORTCUT_NAMES has the names).
SHORTCUT_KEYS = ("dictate", "target", "teach", "offline")


def parse_which(value: str) -> list[str]:
    """``--which``: comma-separated shortcut keys -> list in the canonical order."""
    keys = {key.strip().lower() for key in value.split(",") if key.strip()}
    unknown = sorted(keys - set(SHORTCUT_KEYS))
    if unknown or not keys:
        bad = f" (unknown: {', '.join(unknown)})" if unknown else ""
        raise argparse.ArgumentTypeError(f"choose from {','.join(SHORTCUT_KEYS)}{bad}")
    return [key for key in SHORTCUT_KEYS if key in keys]


def _shortcut_hub_settings(args: argparse.Namespace, paths: Paths) -> tuple[str, str, str | None]:
    """Home Wi-Fi name, LAN URL and public URL (or None) for Dictate and Teach."""
    from shoutabout import netinfo

    ssid = args.ssid
    if ssid is None:
        ssid = netinfo.wifi_ssid()
        if not ssid:
            raise CliError(
                "couldn't read this PC's Wi-Fi name (Ethernet, Wi-Fi off, or Windows needs "
                "Location access for it). Pass --ssid <home-wifi-name>, exactly as the iPhone "
                "shows it in Settings > Wi-Fi."
            )
        print(f"Home Wi-Fi: {ssid!r} (this PC's current Wi-Fi; use --ssid to change it)")
    else:
        print(f"Home Wi-Fi: {ssid!r}")

    lan_url = args.lan_url
    if not lan_url:
        base = netinfo.lan_url(_load_config(paths))
        if not base:
            raise CliError(
                "no LAN URL: [server] host is loopback-only or no private IPv4 address was "
                "found. Pass --lan-url http://<PC-LAN-IP>:<port>/transcribe"
            )
        lan_url = base + "/transcribe"

    if args.no_public:
        public_url = None
    elif args.public_url:
        public_url = args.public_url
    else:
        public_url = _public_transcribe_url(paths)
    return ssid, lan_url, public_url


def cmd_make_shortcuts(args: argparse.Namespace) -> int:
    from shoutabout import shortcuts

    paths = _paths(args)
    which: list[str] = args.which
    names = {key: shortcuts.SHORTCUT_NAMES[key] for key in which}
    print(f"Shortcuts:  {', '.join(names.values())}")

    ssid: str | None = None
    lan_url: str | None = None
    public_url: str | None = None
    needs_hub = bool(shortcuts.NEEDS_HUB.intersection(which))
    if needs_hub:
        ssid, lan_url, public_url = _shortcut_hub_settings(args, paths)

    cherri = args.format == "cherri"
    try:
        if cherri:
            built: dict[str, Any] = {
                key: shortcuts.build_cherri(key, ssid, lan_url, public_url) for key in which
            }
        else:
            built = {key: shortcuts.build(key, ssid, lan_url, public_url) for key in which}
        if lan_url is not None:
            lan_url = shortcuts.transcribe_url(lan_url)
        if public_url is not None:
            public_url = shortcuts.transcribe_url(public_url, https_only=True)
    except ValueError as exc:
        raise CliError(str(exc)) from exc

    if needs_hub:
        print(f"LAN URL:    {lan_url}")
        if public_url:
            print(f"Public URL: {public_url}")
        else:
            why = "--no-public" if args.no_public else "no public_hostname in install-state.json"
            hub_names = [names[key] for key in which if key in shortcuts.NEEDS_HUB]
            plural = len(hub_names) > 1
            does = "show a notification and send" if plural else "shows a notification and sends"
            print(
                f"Public URL: none ({why}). Away from home Wi-Fi, {' and '.join(hub_names)} "
                f"{does} nothing. Use --public-url https://<host>/transcribe to add the tunnel "
                "route."
            )
        if "teach" in which:
            print("Teach ShoutAbout posts to /vocab on the same hosts.")

    files: dict[str, bytes] = {}
    if cherri:
        print(CHERRI_NOTE)
        for key, source in built.items():
            files[f"{names[key]}.cherri"] = source.encode("utf-8")
    elif args.no_sign:
        for key, shortcut in built.items():
            files[f"{names[key]}.plist"] = shortcuts.to_plist_bytes(shortcut)
    else:
        print(SIGNING_NOTE)
        for key, shortcut in built.items():
            name = names[key]
            try:
                signed = shortcuts.sign_with_hubsign(name, shortcuts.to_plist_bytes(shortcut))
            except shortcuts.ShortcutSignError as exc:
                raise CliError(
                    f"signing {name!r} failed: {exc}. Try again in a minute, or use --no-sign "
                    "and sign the .plist files on a Mac, or --format cherri."
                ) from exc
            files[f"{name}.shortcut"] = signed

    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    for filename, data in files.items():
        (out / filename).write_bytes(data)
        print(f"  wrote {out / filename}")
    if cherri:
        print("Next: compile and sign the .cherri files, then add them on the iPhone.")
    elif args.no_sign:
        print("Unsigned: iOS won't import these until they're signed on a Mac.")
    else:
        asking = [names[key] for key in which if key in shortcuts.NEEDS_HUB]
        questions = f", then answer the questions of {' and '.join(asking)}" if asking else ""
        print(
            "Next: get the files onto the iPhone, open each in Shortcuts and add it"
            f"{questions} (docs/ios-shortcuts.md). The files hold no secrets."
        )
    return 0


PAIR_WARNING = (
    "This QR code holds your hub token{cloudflare}. Show it only to your own iPhone; don't "
    "photograph, screenshot or share it. If it leaks: python -m shoutabout init --rotate-token."
)


def _pair_cloudflare(args: argparse.Namespace) -> Any:
    """The Cloudflare service token from --cf-file, the --cloudflare prompt, or the env."""
    from shoutabout import pairing

    if args.cf_file:
        path = Path(args.cf_file).expanduser()
        try:
            text = path.read_text("utf-8-sig")
        except OSError as exc:
            raise CliError(f"can't read {path}: {exc.strerror or exc}") from exc
        return pairing.parse_cf_text(text, source=str(path))
    if args.cloudflare:
        import getpass

        client_id = getpass.getpass("Cloudflare service token Client ID (input hidden): ")
        client_secret = getpass.getpass("Cloudflare service token Client Secret (input hidden): ")
        credentials = pairing.cloudflare_credentials(client_id, client_secret, "the prompt")
        if credentials is None:
            raise CliError("no Cloudflare values entered")
        return credentials
    return pairing.cf_from_env()


def cmd_pair(args: argparse.Namespace) -> int:
    from shoutabout import netinfo, pairing
    from shoutabout.config import load_token

    if args.show_secrets and not args.json:
        raise CliError("--show-secrets only works with --json")
    paths = _paths(args)
    token = load_token(paths)

    lan_url = args.lan_url or netinfo.lan_url(_load_config(paths))
    if not lan_url:
        raise CliError(
            "no LAN URL: [server] host is loopback-only or no private IPv4 address was found. "
            "Pass --lan-url http://<PC-LAN-IP>:<port>"
        )
    if args.no_public:
        public_url = None
    elif args.public_url:
        public_url = args.public_url
    else:
        public_url = _public_transcribe_url(paths)
    ssid = args.ssid if args.ssid is not None else netinfo.wifi_ssid()

    try:
        cloudflare = _pair_cloudflare(args)
        payload = pairing.build_payload(
            lan_url=lan_url, token=token, public_url=public_url, cloudflare=cloudflare, ssid=ssid
        )
    except pairing.PairingError as exc:
        raise CliError(str(exc)) from exc

    if args.json:
        shown = payload if args.show_secrets else pairing.redacted(payload)
        print(pairing.to_json(shown))
        return 0

    print("Pairing QR code for the ShoutAbout iPhone app:")
    print(f"  LAN URL:    {payload['lan_url']}")
    print(f"  Public URL: {payload['public_url'] or 'none'}")
    if payload["ssid"]:
        print(f"  Home Wi-Fi: {payload['ssid']!r}")
    else:
        print("  Home Wi-Fi: not detected (pass --ssid <home-wifi-name> to include it)")
    print("  Hub token:  included")
    if cloudflare is not None:
        print("  Cloudflare: service token included")
        if not cloudflare.client_id.endswith(".access"):
            print("warning: a Cloudflare Client ID normally ends in .access", file=sys.stderr)
        if payload["public_url"] is None:
            print("warning: Cloudflare values without a public URL are unused", file=sys.stderr)
    else:
        print("  Cloudflare: not included (--cloudflare, --cf-file or SHOUTABOUT_CF_CLIENT_*)")
    with_cf = " and Cloudflare service token" if cloudflare is not None else ""
    print(PAIR_WARNING.format(cloudflare=with_cf))

    text = pairing.to_json(payload)
    if args.open:
        return _open_qr_svg(text)
    print_qr(text)
    print("Scan it with the ShoutAbout app on the iPhone, then clear this window.")
    return 0


def cmd_transcribe(args: argparse.Namespace) -> int:
    files, problems = expand_inputs(args.files)
    for problem in problems:
        _err(problem)
    if not files:
        return 1

    from shoutabout.engine import create_engine
    from shoutabout.engine.base import EngineError
    from shoutabout.inject.base import NullInjector
    from shoutabout.modelpick import resolve
    from shoutabout.pipeline import Pipeline
    from shoutabout.state import HubState
    from shoutabout.vocab import VocabStore

    paths = _paths(args)
    cfg = _load_config(paths)
    choice = resolve(cfg, probe_ollama=not args.no_cleanup)
    cleaner = None
    if choice.cleanup and choice.cleanup_model and not args.no_cleanup:
        from shoutabout.cleanup import OllamaCleaner

        cleaner = OllamaCleaner(cfg.cleanup.model_copy(update={"model": choice.cleanup_model}))

    engine = create_engine(cfg.stt)
    if not args.json:
        print(f"loading {choice.stt_model} on {choice.device}...", file=sys.stderr)
    try:
        engine.load()
    except EngineError as exc:
        raise CliError(str(exc)) from exc
    pipeline = Pipeline(
        config=cfg,
        engine=engine,
        vocab=VocabStore(paths.vocab_file),
        cleaner=cleaner,
        injector=NullInjector("The CLI never pastes."),
        state=HubState(),
        history=None,
    )

    manifests: dict[Path, dict[str, dict[str, Any]]] = {}
    failures = 0
    for path in files:
        directory = path.resolve().parent
        if directory not in manifests:
            manifests[directory] = load_manifest(directory)
        entry = manifests[directory].get(path.name.lower())
        try:
            result = pipeline.process_file(
                path,
                target="phone",
                route="cli",
                cleanup=False if args.no_cleanup else None,
                deliver=False,
            )
        except Exception as exc:
            failures += 1
            if args.json:
                print(json.dumps({"file": str(path), "error": str(exc)}, ensure_ascii=False))
            else:
                print(f"== {path}\n   error: {type(exc).__name__}: {exc}")
            continue
        evaluation = (
            evaluate_against_manifest(entry, result.raw_text, result.text) if entry else None
        )
        if args.json:
            record: dict[str, Any] = {
                "file": str(path),
                "raw_text": result.raw_text,
                "text": result.text,
                "cleanup": result.cleanup.status,
                "cleanup_reason": result.cleanup.reason,
                "audio_seconds": round(result.audio_seconds, 2),
                "timings_ms": result.timings.as_dict(),
            }
            if evaluation:
                record.update(evaluation)
            print(json.dumps(record, ensure_ascii=False))
        else:
            _print_transcription(path, result, evaluation)
    return 1 if failures else 0


def _print_transcription(path: Path, result: Any, evaluation: dict[str, Any] | None) -> None:
    t = result.timings.as_dict()
    reason = f" ({result.cleanup.reason})" if result.cleanup.reason else ""
    print(f"== {path}  [{result.audio_seconds:.1f} s audio]")
    print(f"   raw:      {result.raw_text}")
    print(f"   cleaned:  {result.text}")
    print(f"   cleanup:  {result.cleanup.status}{reason}")
    print(
        f"   timings:  decode={t['decode']} stt={t['stt']} cleanup={t['cleanup']} "
        f"total={t['total']} ms"
    )
    if evaluation:
        print(f"   expected: {evaluation['expected']}")
        print(
            f"   accuracy: raw {evaluation['word_accuracy_raw']:.1%}, "
            f"cleaned {evaluation['word_accuracy']:.1%} (word match)"
        )
        if evaluation["terms"]:
            hits = ", ".join(
                f"{term} [{'hit' if ok else 'MISS'}]" for term, ok in evaluation["terms"].items()
            )
            print(f"   terms:    {hits}")


def cmd_benchmark(args: argparse.Namespace) -> int:
    from shoutabout.benchmark import run_llm_benchmark, run_stt_benchmark

    paths = _paths(args)
    cfg = _load_config(paths)
    rc = 0
    if args.stt:
        files, problems = expand_inputs(args.stt)
        for problem in problems:
            _err(problem)
        if not files:
            return 1
        rc |= run_stt_benchmark(files, cfg, args.runs, vocab_path=paths.vocab_file)
    if args.models or not args.stt:
        from shoutabout.benchmark import default_terms
        from shoutabout.vocab import VocabStore

        models = [m.strip() for m in (args.models or cfg.cleanup.model).split(",") if m.strip()]
        # The built-in samples use the default terms; add the user's own vocab after them.
        terms = list(dict.fromkeys([*default_terms(), *VocabStore(paths.vocab_file).get().terms]))
        rc |= run_llm_benchmark(models, args.runs, cfg.cleanup, terms=terms)
    return 1 if rc else 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from shoutabout.doctor import run_doctor

    return run_doctor(
        _paths(args),
        skip_model=args.skip_model,
        skip_tunnel=args.skip_tunnel,
        skip_ollama=args.skip_ollama,
        url=args.url,
        cf_client_id=args.cf_client_id or os.environ.get("SHOUTABOUT_CF_CLIENT_ID"),
        cf_client_secret=args.cf_client_secret or os.environ.get("SHOUTABOUT_CF_CLIENT_SECRET"),
    )


def cmd_download_model(args: argparse.Namespace) -> int:
    from shoutabout import modelstore
    from shoutabout.modelpick import resolve

    cfg = _load_config(_paths(args))
    choice = resolve(cfg, probe_ollama=False)  # "auto" -> the model this PC will run
    name = choice.stt_model
    shown = {"last": -1}

    def progress(done: int, total: int) -> None:
        pct = int(done * 100 / total) if total else 0
        if pct // 10 != shown["last"] // 10 or pct == 100:
            shown["last"] = pct
            print(f"  {pct:3d}%  {done / 1e6:,.0f} / {total / 1e6:,.0f} MB", flush=True)

    print(f"downloading Whisper model {name!r} ({choice.label})...")
    try:
        path = modelstore.download(name, progress=progress, cache_dir=cfg.stt.download_root or None)
    except modelstore.ModelStoreError as exc:
        raise CliError(str(exc)) from exc
    print(f"model ready: {path}")
    return 0


def cmd_encode_m4a(args: argparse.Namespace) -> int:
    from shoutabout.audio import AudioError, encode_m4a

    src, dst = Path(args.src), Path(args.dst)
    if not src.is_file():
        raise CliError(f"not found: {src}")
    try:
        encode_m4a(src, dst, bitrate=args.bitrate)
    except AudioError as exc:  # e.g. the installed app, which has no PyAV
        raise CliError(str(exc)) from exc
    print(f"wrote {dst}")
    return 0


# ---------------------------------------------------------------------- vocab


def _vocab_term(args: argparse.Namespace) -> str:
    # `vocab add Chameleon CV` is one phrase: the shell splits it, we join it back.
    return " ".join(args.term)


def _learn_roots(paths: Paths, given: Sequence[str] = ()) -> list[Path] | None:
    if given:
        roots = [Path(r).expanduser() for r in given]
        missing = [str(r) for r in roots if not r.is_dir()]
        if missing:
            raise CliError(f"not a folder: {', '.join(missing)}")
        return roots
    configured = _load_config(paths).learn.project_roots
    return [Path(r).expanduser() for r in configured] or None


def _print_suggestions(paths: Paths, new: int, *, json_out: bool, source: str | None) -> None:
    from shoutabout.learn import pending_suggestions
    from shoutabout.review_ui import group_suggestions

    pending = [s for s in pending_suggestions(paths) if source is None or s.source == source]
    if json_out:
        out = {"new": new, "pending": [s.to_dict() for s in pending]}
        print(json.dumps(out, ensure_ascii=False))
        return
    print(f"{new} new suggestion(s); {len(pending)} waiting for review.")
    width = max((len(s.term) for s in pending), default=0)
    for title, items in group_suggestions(pending):
        print(f"\n  {title}")
        for s in items:
            print(f"    {s.term.ljust(width)}  {s.detail}")
    if pending:
        print(
            '\nTeach one: shoutabout vocab add "<term>"   Dismiss: shoutabout vocab dismiss '
            '"<term>"   All at once: shoutabout vocab review'
        )


def cmd_vocab_list(args: argparse.Namespace) -> int:
    from shoutabout.vocab import read_terms

    paths = _paths(args)
    terms = read_terms(paths.vocab_file)
    if args.json:
        print(json.dumps({"terms": terms, "count": len(terms)}, ensure_ascii=False))
        return 0
    for i, term in enumerate(terms, 1):
        print(f"{i:>3}. {term}")
    print(f"{len(terms)} term(s) in {paths.vocab_file} (top = highest priority)")
    return 0


def cmd_vocab_add(args: argparse.Namespace) -> int:
    from shoutabout.learn import accept

    paths = _paths(args)
    result = accept(paths, [_vocab_term(args)])[0]
    if result.status == "invalid":
        raise CliError(f"can't add that: {result.reason}")
    if result.status == "added":
        n = result.count
        print(f'added "{result.term}" at the top of {paths.vocab_file} ({n} term{"s" * (n != 1)})')
    else:
        print(f'"{result.term}" is already in {paths.vocab_file}')
    return 0


def cmd_vocab_remove(args: argparse.Namespace) -> int:
    from shoutabout.vocab import remove_term

    paths = _paths(args)
    term = _vocab_term(args)
    if not remove_term(paths.vocab_file, term):
        raise CliError(f'"{term}" is not in {paths.vocab_file}')
    print(f'removed "{term}" from {paths.vocab_file}')
    return 0


def cmd_vocab_dismiss(args: argparse.Namespace) -> int:
    from shoutabout.learn import dismiss

    paths = _paths(args)
    term = _vocab_term(args)
    dismiss(paths, [term])
    print(f'"{term}" won\'t be suggested again')
    return 0


def cmd_vocab_suggest(args: argparse.Namespace) -> int:
    from shoutabout.learn import refresh_suggestions

    paths = _paths(args)
    roots = _learn_roots(paths) if args.projects else None
    history_on = _load_config(paths).history.enabled  # history is mined only while it's on
    if not history_on:
        print(
            "History is off, so your dictations weren't scanned. Turn it on in Settings.",
            file=sys.stderr,
        )
    new = refresh_suggestions(
        paths, include_projects=args.projects, include_history=history_on, roots=roots
    )
    _print_suggestions(paths, new, json_out=args.json, source=None)
    return 0


def cmd_vocab_seed(args: argparse.Namespace) -> int:
    from shoutabout.learn import refresh_suggestions

    paths = _paths(args)
    roots = _learn_roots(paths, args.roots)
    new = refresh_suggestions(paths, include_projects=True, include_history=False, roots=roots)
    _print_suggestions(paths, new, json_out=args.json, source="projects")
    return 0


def cmd_vocab_review(args: argparse.Namespace) -> int:
    from shoutabout.review_ui import run_review_window

    run_review_window(_paths(args))
    return 0


# ------------------------------------------------------- setup / settings / devices


def standalone_services(paths: Paths) -> Any:
    """AppServices without a hub in this process (pairing happens in the running hub)."""
    from shoutabout import __version__
    from shoutabout.app_services import AppServices
    from shoutabout.config import ensure_initialized
    from shoutabout.devices import DeviceStore
    from shoutabout.identity import IdentityStore
    from shoutabout.winstartup import StartupManager

    ensure_initialized(paths)
    return AppServices(
        paths=paths,
        devices=DeviceStore(paths.devices_file),
        identity=IdentityStore(paths.identity_file),
        startup=StartupManager(),
        version=__version__,
    )


def cmd_setup(args: argparse.Namespace) -> int:
    if sys.platform == "darwin":  # v2-macos: Tk must run on the main thread
        from shoutabout.mac.ui_child import run_standalone

        return run_standalone(_paths(args), "setup", "phone" if args.phone else "full")
    from shoutabout.ui_host import UiHost

    host = UiHost(standalone_services(_paths(args)))
    host.open_setup("phone" if args.phone else "full")
    host.wait()
    return 0


def cmd_settings(args: argparse.Namespace) -> int:
    if sys.platform == "darwin":  # v2-macos
        from shoutabout.mac.ui_child import run_standalone

        return run_standalone(_paths(args), "settings", None)
    from shoutabout.ui_host import UiHost

    host = UiHost(standalone_services(_paths(args)))
    host.svc.open_history = host.open_history
    host.open_settings()
    host.wait()
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    from shoutabout.ui_host import UiHost

    host = UiHost(standalone_services(_paths(args)))
    host.svc.open_history = host.open_history
    host.open_history()
    host.wait()
    return 0


def cmd_mac_ui(args: argparse.Namespace) -> int:
    """v2-macos: the window process the Mac hub starts (never run by hand)."""
    from shoutabout.mac.ui_child import run_child

    return run_child(_paths(args))


def cmd_mac_teach(args: argparse.Namespace) -> int:
    """v2-macos: the Teach window in its own process."""
    from shoutabout.mac.ui_child import run_teach

    return run_teach(_paths(args))


def _find_device(devices: Sequence[Any], which: str) -> Any:
    which = which.strip()
    exact = [d for d in devices if d.key == which or d.name == which]
    if exact:
        return exact[0]
    prefixed = [d for d in devices if len(which) >= 4 and d.key.startswith(which)]
    if len(prefixed) == 1:
        return prefixed[0]
    if len(prefixed) > 1:
        raise CliError(f"{which!r} matches more than one phone; give more of its id")
    raise CliError(f"no paired phone called {which!r} (see `devices list`)")


def cmd_devices_list(args: argparse.Namespace) -> int:
    from shoutabout.devices import DeviceStore

    devices = DeviceStore(_paths(args).devices_file).list()
    if args.json:
        rows = [
            {
                "id": d.key,
                "name": d.name,
                "created": d.created,
                "last_seen": d.last_seen,
            }
            for d in devices
        ]
        print(json.dumps({"devices": rows}, ensure_ascii=False))
        return 0
    if not devices:
        print("No phones are paired. Pair one: Settings… > Add a phone.")
        return 0
    for d in devices:
        print(f"{d.key[:8]}  {d.name}  (paired {d.created or '?'}, last used {d.last_seen or '-'})")
    return 0


def cmd_devices_remove(args: argparse.Namespace) -> int:
    from shoutabout.devices import DeviceStore

    store = DeviceStore(_paths(args).devices_file)
    device = _find_device(store.list(), " ".join(args.device))
    store.remove(device.key)
    print(f"removed {device.name!r}: its token stops working now")
    return 0


def _add_vocab_parser(sub: Any, common: argparse.ArgumentParser) -> None:
    p = sub.add_parser(
        "vocab", parents=[common], help="teach words: list/add/remove, suggestions, review"
    )
    vsub = p.add_subparsers(dest="vocab_command", metavar="ACTION")
    vsub.required = True
    term_help = "the word or phrase (several words are joined: vocab add Chameleon CV)"

    q = vsub.add_parser("list", parents=[common], help="show vocab.txt in priority order")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_vocab_list)

    q = vsub.add_parser("add", parents=[common], help="teach a word (it goes to the top)")
    q.add_argument("term", nargs="+", metavar="TERM", help=term_help)
    q.set_defaults(func=cmd_vocab_add)

    q = vsub.add_parser("remove", parents=[common], help="remove a word from vocab.txt")
    q.add_argument("term", nargs="+", metavar="TERM", help=term_help)
    q.set_defaults(func=cmd_vocab_remove)

    q = vsub.add_parser("dismiss", parents=[common], help="never suggest a word again")
    q.add_argument("term", nargs="+", metavar="TERM", help=term_help)
    q.set_defaults(func=cmd_vocab_dismiss)

    q = vsub.add_parser(
        "suggest", parents=[common], help="find words you say often (history; + projects)"
    )
    q.add_argument("--projects", action="store_true", help="also scan your project folders")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_vocab_suggest)

    q = vsub.add_parser("seed", parents=[common], help="suggest names from project folders")
    q.add_argument(
        "roots",
        nargs="*",
        metavar="ROOT",
        help="folders to scan 2 levels deep (default: [learn] project_roots, else "
        "~/Documents, ~/source/repos, ~/repos, ~/code, ~/projects)",
    )
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_vocab_seed)

    q = vsub.add_parser("review", parents=[common], help="open the Teach ShoutAbout window")
    q.set_defaults(func=cmd_vocab_review)


# --------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--home",
        metavar="PATH",
        default=argparse.SUPPRESS,
        help="config dir (overrides SHOUTABOUT_HOME; default %%APPDATA%%\\shoutabout)",
    )
    parser = argparse.ArgumentParser(
        prog="shoutabout",
        description="iPhone -> PC voice dictation hub.",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"shoutabout {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    p = sub.add_parser("run", parents=[common], help="run the hub (tray + server)")
    p.add_argument("--no-tray", action="store_true", help="no tray icon; log to the console")
    # The user opened the app (not sign-in): say it's ready, or show the running copy.
    p.add_argument("--greet", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("init", parents=[common], help="create config, vocab and hub token")
    p.add_argument("--rotate-token", action="store_true", help="replace the hub token")
    p.add_argument("--port", type=int, help="set [server] port in config.toml")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("info", parents=[common], help="show paths and settings (no secrets)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("token", parents=[common], help="print / copy / QR the hub token")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--copy", action="store_true", help="copy (excluded from history)")
    group.add_argument("--qr", action="store_true", help="show as a terminal QR code")
    p.set_defaults(func=cmd_token)

    p = sub.add_parser("qr", parents=[common], help="show text as a QR code (move secrets)")
    p.add_argument("text", nargs="?", help="text to encode")
    p.add_argument("--clipboard", action="store_true", help="encode the clipboard text")
    p.add_argument("--open", action="store_true", help="open as an SVG image; deleted after Enter")
    p.set_defaults(func=cmd_qr)

    p = sub.add_parser(
        "make-shortcuts", parents=[common], help="build the iPhone shortcuts (signed files)"
    )
    p.add_argument(
        "--ssid", metavar="NAME", help="home Wi-Fi name (default: this PC's current Wi-Fi)"
    )
    p.add_argument("--lan-url", metavar="URL", help="default: lan_url from `info` + /transcribe")
    group = p.add_mutually_exclusive_group()
    group.add_argument(
        "--public-url",
        metavar="URL",
        help="default: https://<public_hostname>/transcribe from install-state.json",
    )
    group.add_argument(
        "--no-public", action="store_true", help="LAN only: no request away from home Wi-Fi"
    )
    p.add_argument(
        "--out", metavar="DIR", default="shortcuts-out", help="output folder (default: %(default)s)"
    )
    p.add_argument(
        "--no-sign",
        action="store_true",
        help="write unsigned .plist files (sign on a Mac) instead of using HubSign",
    )
    p.add_argument(
        "--which",
        metavar="LIST",
        type=parse_which,
        default=",".join(SHORTCUT_KEYS),
        help="comma-separated: dictate,target,teach,offline (default: all four)",
    )
    p.add_argument(
        "--format",
        choices=("shortcut", "cherri"),
        default="shortcut",
        help="shortcut: signed .shortcut files (.plist with --no-sign); cherri: Cherri source "
        "to compile and sign with the official Cherri playground or compiler (not yet run on "
        "an iPhone)",
    )
    p.set_defaults(func=cmd_make_shortcuts)

    p = sub.add_parser(
        "pair", parents=[common], help="QR code that pairs the ShoutAbout iPhone app (secrets!)"
    )
    p.add_argument(
        "--ssid", metavar="NAME", help="home Wi-Fi name (default: this PC's current Wi-Fi)"
    )
    p.add_argument("--lan-url", metavar="URL", help="default: lan_url from `info`")
    group = p.add_mutually_exclusive_group()
    group.add_argument(
        "--public-url",
        metavar="URL",
        help="default: https://<public_hostname> from install-state.json",
    )
    group.add_argument("--no-public", action="store_true", help="LAN only: no public URL")
    group = p.add_mutually_exclusive_group()
    group.add_argument(
        "--cloudflare",
        action="store_true",
        help="ask for the Cloudflare service token (hidden input)",
    )
    group.add_argument(
        "--cf-file",
        metavar="PATH",
        help="read 'CF-Access-Client-Id: ...' / 'CF-Access-Client-Secret: ...' lines from a file",
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument(
        "--open", action="store_true", help="open as an SVG image; deleted after Enter"
    )
    group.add_argument(
        "--json", action="store_true", help="print the payload as JSON (secrets hidden)"
    )
    p.add_argument("--show-secrets", action="store_true", help="with --json: include the secrets")
    p.set_defaults(func=cmd_pair)

    p = sub.add_parser("transcribe", parents=[common], help="transcribe files (never pastes)")
    p.add_argument("files", nargs="+", metavar="FILE", help="audio files, folders or globs")
    p.add_argument("--no-cleanup", action="store_true", help="skip the LLM cleanup")
    p.add_argument("--json", action="store_true", help="one JSON object per file")
    p.set_defaults(func=cmd_transcribe)

    p = sub.add_parser("benchmark", parents=[common], help="benchmark cleanup LLMs / STT")
    p.add_argument("--models", help="comma-separated Ollama models (default: configured)")
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--stt", nargs="+", metavar="FILE", help="benchmark STT on these files")
    p.set_defaults(func=cmd_benchmark)

    _add_vocab_parser(sub, common)

    p = sub.add_parser("setup", parents=[common], help="open the setup window")
    p.add_argument("--phone", action="store_true", help="only the add-a-phone step")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("settings", parents=[common], help="open the settings window")
    p.set_defaults(func=cmd_settings)

    p = sub.add_parser("history", parents=[common], help="open the History window")
    p.set_defaults(func=cmd_history)
    # v2-macos: internal commands of the Mac app (its window processes)
    p = sub.add_parser("mac-ui", parents=[common], help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_mac_ui)
    p = sub.add_parser("mac-teach", parents=[common], help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_mac_teach)

    p = sub.add_parser("devices", parents=[common], help="paired phones: list / remove")
    dsub = p.add_subparsers(dest="devices_command", metavar="ACTION")
    dsub.required = True
    q = dsub.add_parser("list", parents=[common], help="show paired phones (no tokens)")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_devices_list)
    q = dsub.add_parser("remove", parents=[common], help="unpair a phone (revokes its token)")
    q.add_argument("device", nargs="+", metavar="NAME_OR_ID", help="its name, or its id")
    q.set_defaults(func=cmd_devices_remove)

    p = sub.add_parser("doctor", parents=[common], help="check the whole setup")
    p.add_argument("--skip-model", action="store_true", help="don't load the Whisper model")
    p.add_argument("--skip-ollama", action="store_true", help="don't check Ollama")
    p.add_argument("--skip-tunnel", action="store_true", help="don't check cloudflared/public URL")
    p.add_argument("--url", help="public hostname or URL (default: from install-state.json)")
    p.add_argument("--cf-client-id", help="Access service token Client ID")
    p.add_argument("--cf-client-secret", help="Access service token Client Secret")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("download-model", parents=[common], help="fetch the Whisper model")
    p.set_defaults(func=cmd_download_model)

    p = sub.add_parser("encode-m4a", parents=[common], help="encode audio as AAC .m4a (dev)")
    p.add_argument("src")
    p.add_argument("dst")
    p.add_argument("--bitrate", type=int, default=64_000)
    p.set_defaults(func=cmd_encode_m4a)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _safe_streams()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help / usage errors
        return exc.code if isinstance(exc.code, int) else 2
    try:
        return int(args.func(args) or 0)
    except CliError as exc:
        _err(str(exc))
        return 1
    except ConfigError as exc:
        _err(str(exc))
        return 1
    except KeyboardInterrupt:
        return 130
