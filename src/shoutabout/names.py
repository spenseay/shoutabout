"""Clean up names that are shown to the user or announced on the network.

Device names come from any phone on the Wi-Fi (``POST /pair/request``), so they're
untrusted: control characters, bidi overrides (which can make "Evil" render as "liv E")
and zero-width characters are dropped, whitespace is collapsed, and the length is capped
before the name reaches an approval prompt, a log line or devices.json.
"""

from __future__ import annotations

import re
import unicodedata

_WS = re.compile(r"\s+")


def clean_name(raw: object, *, max_chars: int = 60, max_utf8_bytes: int | None = None) -> str:
    """``raw`` as a safe one-line name ("" if nothing printable is left).

    Drops Unicode control (Cc) and format (Cf: bidi overrides, zero-width joiners, BOM)
    characters and unpaired surrogates, collapses whitespace, then cuts to ``max_chars``
    characters and, optionally, ``max_utf8_bytes`` bytes of UTF-8 (a DNS label is 63).
    """
    if not isinstance(raw, str):
        return ""
    kept = []
    for ch in raw:
        if ch.isspace():
            kept.append(" ")
            continue
        if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn"):
            continue
        kept.append(ch)
    text = _WS.sub(" ", "".join(kept)).strip()
    text = text[:max_chars].rstrip()
    if max_utf8_bytes is not None:
        while text and len(text.encode("utf-8")) > max_utf8_bytes:
            text = text[:-1]
        text = text.rstrip()
    return text
