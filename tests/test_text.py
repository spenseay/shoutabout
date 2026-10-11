from __future__ import annotations

import pytest

from shoutabout.text import normalize_whitespace


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("hello world", "hello world"),
        ("  hello   world  ", "hello world"),
        ("line one\nline two", "line one line two"),
        ("line one\r\nline two\r\n", "line one line two"),
        ("a\rb", "a b"),
        ("a\tb\t\tc", "a b c"),
        ("a b c", "a b c"),
        ("a b", "a b"),
        ("a b　c", "a b c"),
        ("a\x0bb\x0cc", "a b c"),
        ("a\x85b", "a b"),
        ("zero​width‌‍⁠﻿", "zerowidth"),
        ("﻿BOM start", "BOM start"),
        ("ctrl\x00chars\x07\x1b[31m", "ctrlchars[31m"),
        ("del\x7fchar", "delchar"),
        ("", ""),
        ("   \n\t ", ""),
        ("ünïcödé — “quotes” ✓", "ünïcödé — “quotes” ✓"),
        ("C# and F#", "C# and F#"),
    ],
)
def test_normalize_whitespace(raw, expected):
    assert normalize_whitespace(raw) == expected


def test_none_like_empty():
    assert normalize_whitespace(None) == ""  # type: ignore[arg-type]


def test_never_contains_newline_like_chars():
    nasty = "".join(chr(c) for c in range(0, 0x3000) if chr(c).isspace() or c < 32) + "x"
    out = normalize_whitespace(f"a{nasty}b\n\r  c")
    for ch in "\n\r\x0b\x0c\x1c\x1d\x1e\x85  \t":
        assert ch not in out
    assert out == "a xb c"


def test_idempotent():
    s = "  Hello,\n\nworld   again\t"
    once = normalize_whitespace(s)
    assert normalize_whitespace(once) == once
