"""Preview the homepage (site/) locally, the way Cloudflare Pages serves it.

    python scripts\\site\\serve.py [--port 18400]

Then open http://127.0.0.1:18400/. Stdlib only. Like Pages, it:
- serves /download from download.html and redirects /download.html to /download;
- answers unknown paths with 404.html and status 404;
- adds the response headers from site/_headers (so the Content-Security-Policy is tested
  too). Only exact paths and a trailing ``*`` splat are supported, which is all the file
  uses.

It binds to 127.0.0.1 only and never writes anything.
"""

from __future__ import annotations

import argparse
import fnmatch
import http.server
from pathlib import Path

SITE = Path(__file__).resolve().parents[2] / "site"


def parse_headers_file(path: Path) -> list[tuple[str, list[tuple[str, str]]]]:
    """``_headers`` rules: a path pattern line, then indented ``Name: value`` lines."""
    rules: list[tuple[str, list[tuple[str, str]]]] = []
    if not path.exists():
        return rules
    for raw in path.read_text("utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw[0] in " \t":
            if rules and ":" in raw:
                name, value = raw.strip().split(":", 1)
                rules[-1][1].append((name.strip(), value.strip()))
        else:
            rules.append((raw.strip(), []))
    return rules


class PagesHandler(http.server.SimpleHTTPRequestHandler):
    rules: list[tuple[str, list[tuple[str, str]]]] = []

    def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        super().__init__(*args, directory=str(SITE), **kwargs)

    def end_headers(self) -> None:
        path = self.path.split("?", 1)[0]
        for pattern, headers in self.rules:
            if fnmatch.fnmatchcase(path, pattern):
                for name, value in headers:
                    self.send_header(name, value)
        super().end_headers()

    def send_head(self):  # type: ignore[no-untyped-def]
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path.endswith(".html"):
            pretty = path[: -len(".html")]
            pretty = "/" if pretty in ("/index", "") else pretty
            self.send_response(308)
            self.send_header("Location", pretty)
            self.end_headers()
            return None
        target = SITE / path.lstrip("/")
        if path != "/" and not target.exists() and (SITE / f"{path.lstrip('/')}.html").is_file():
            self.path = f"{path}.html"
        elif not (target.is_file() or (target.is_dir() and (target / "index.html").is_file())):
            body = (SITE / "404.html").read_bytes()
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.path = "/404.html"
            self.end_headers()
            self.wfile.write(body)
            return None
        return super().send_head()


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview site/ like Cloudflare Pages.")
    parser.add_argument("--port", type=int, default=18400)
    args = parser.parse_args()
    PagesHandler.rules = parse_headers_file(SITE / "_headers")
    PagesHandler.extensions_map[".webmanifest"] = "application/manifest+json"
    PagesHandler.extensions_map[".webp"] = "image/webp"
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), PagesHandler)
    print(f"Serving {SITE} on http://127.0.0.1:{args.port}/ (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
