"""Grapheme-cluster counting for the contest reason bound (10-2000 clusters).

The Flutter client measures the same text with ``package:characters`` (full UAX #29), so
the server needs a compatible notion of "one user-perceived character" — counting code
points would reject a 2000-character reason written with accents or emoji.

This is a reduced UAX #29 implementation covering the rules that real contest text
exercises: CRLF, control characters, combining marks, spacing marks, ZWJ emoji
sequences, emoji modifiers/variation selectors, regional-indicator flag pairs and Hangul
syllables. Contract v1 documents this scope; the shared boundary vectors stay inside it.
"""

from __future__ import annotations

import unicodedata
from typing import Final

_ZWJ: Final = 0x200D
_CR: Final = 0x000D
_LF: Final = 0x000A

_PREPEND: Final[frozenset[int]] = frozenset(
    {*range(0x0600, 0x0606), 0x06DD, 0x070F, 0x0890, 0x0891, 0x08E2, 0x0D4E, 0x110BD, 0x110CD}
)

_REGIONAL_INDICATORS: Final = range(0x1F1E6, 0x1F200)

#: Emoji_Modifier (skin tones) are Extend under UAX #29 but carry category So.
_EMOJI_MODIFIERS: Final = range(0x1F3FB, 0x1F400)

_EXTENDED_PICTOGRAPHIC_RANGES: Final[tuple[tuple[int, int], ...]] = (
    (0x00A9, 0x00A9),
    (0x00AE, 0x00AE),
    (0x203C, 0x3299),
    (0x1F000, 0x1FAFF),
    (0x1FC00, 0x1FFFD),
)

_HANGUL_L: Final = range(0x1100, 0x1160)
_HANGUL_V: Final = range(0x1160, 0x11A8)
_HANGUL_T: Final = range(0x11A8, 0x1200)
_HANGUL_SYLLABLES: Final = range(0xAC00, 0xD7A4)


def _is_extend(code: int) -> bool:
    if code in _EMOJI_MODIFIERS or code in (0xFE0E, 0xFE0F):
        return True
    return unicodedata.category(chr(code)) in {"Mn", "Me"}


def _is_spacing_mark(code: int) -> bool:
    return unicodedata.category(chr(code)) == "Mc"


def _is_control(code: int) -> bool:
    if code in (_CR, _LF):
        return True
    category = unicodedata.category(chr(code))
    return category in {"Zl", "Zp"} or (category in {"Cc", "Cs", "Cf"} and code != _ZWJ)


def _is_extended_pictographic(code: int) -> bool:
    return any(low <= code <= high for low, high in _EXTENDED_PICTOGRAPHIC_RANGES)


def _is_hangul_lv(code: int) -> bool:
    return code in _HANGUL_SYLLABLES and (code - 0xAC00) % 28 == 0


def _is_hangul_lvt(code: int) -> bool:
    return code in _HANGUL_SYLLABLES and (code - 0xAC00) % 28 != 0


def _breaks_between(
    previous: int,
    current: int,
    *,
    pictographic_pending: bool,
    regional_run: int,
) -> bool:
    if previous == _CR and current == _LF:
        return False  # GB3
    if _is_control(previous) or _is_control(current):
        return True  # GB4 / GB5
    if previous in _HANGUL_L and (
        current in _HANGUL_L or current in _HANGUL_V or _is_hangul_lv(current) or _is_hangul_lvt(current)
    ):
        return False  # GB6
    if (previous in _HANGUL_V or _is_hangul_lv(previous)) and (current in _HANGUL_V or current in _HANGUL_T):
        return False  # GB7
    if (previous in _HANGUL_T or _is_hangul_lvt(previous)) and current in _HANGUL_T:
        return False  # GB8
    if _is_extend(current) or current == _ZWJ:
        return False  # GB9
    if _is_spacing_mark(current):
        return False  # GB9a
    if previous in _PREPEND:
        return False  # GB9b
    if previous == _ZWJ and pictographic_pending and _is_extended_pictographic(current):
        return False  # GB11
    # GB12 / GB13: regional indicators pair up, so only an odd-length run continues.
    return not (
        previous in _REGIONAL_INDICATORS and current in _REGIONAL_INDICATORS and regional_run % 2 == 1
    )


def grapheme_clusters(text: str) -> list[str]:
    """Split ``text`` into user-perceived characters."""
    if not text:
        return []
    clusters: list[str] = []
    current = text[0]
    pictographic_pending = _is_extended_pictographic(ord(text[0]))
    regional_run = 1 if ord(text[0]) in _REGIONAL_INDICATORS else 0

    for char in text[1:]:
        previous_code = ord(current[-1])
        code = ord(char)
        if _breaks_between(
            previous_code,
            code,
            pictographic_pending=pictographic_pending,
            regional_run=regional_run,
        ):
            clusters.append(current)
            current = char
            pictographic_pending = _is_extended_pictographic(code)
            regional_run = 1 if code in _REGIONAL_INDICATORS else 0
        else:
            current += char
            if _is_extended_pictographic(code):
                pictographic_pending = True
            elif not (_is_extend(code) or code == _ZWJ):
                pictographic_pending = False
            if code in _REGIONAL_INDICATORS:
                regional_run += 1
    clusters.append(current)
    return clusters


def grapheme_length(text: str) -> int:
    """Number of user-perceived characters in ``text``."""
    return len(grapheme_clusters(text))
