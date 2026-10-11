"""Text normalisation shared by the pipeline and cleanup stage."""

from __future__ import annotations

import re
import unicodedata

# Zero-width and BOM characters that should never reach a paste target.
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)
_WS = re.compile(r"\s+")


def normalize_whitespace(text: str) -> str:
    """Collapse all whitespace (incl. newlines, tabs, NBSP, U+2028/2029) to single spaces.

    v1 output is always a single paragraph with no newline characters, because a
    newline pasted into a terminal can submit a prompt early. Control characters
    are dropped as well.
    """
    if not text:
        return ""
    text = text.translate(_INVISIBLE)
    cleaned = []
    for ch in text:
        if ch.isspace():
            cleaned.append(" ")
        elif unicodedata.category(ch) == "Cc":
            continue  # other control characters (e.g. \x00, \x1b)
        else:
            cleaned.append(ch)
    return _WS.sub(" ", "".join(cleaned)).strip()
