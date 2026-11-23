"""Neutralise untrusted text (file and folder names) before it is printed in tool output."""

from __future__ import annotations

import re
import unicodedata

# Bidirectional overrides/isolates can make text display differently from what it says.
_BIDI_CONTROLS = re.compile("[\\u202a-\\u202e\\u2066-\\u2069]")


def clean_text(value: object) -> str:
    """Make untrusted text (file and folder names) safe to place on a single output line.

    A name containing a newline could otherwise fake extra listing lines or instructions.
    """
    text = _BIDI_CONTROLS.sub(" ", str(value))
    return "".join(" " if unicodedata.category(ch) == "Cc" or ch in "\u2028\u2029" else ch for ch in text)
