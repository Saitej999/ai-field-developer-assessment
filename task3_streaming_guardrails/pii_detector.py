"""Pattern-based PII redaction for complete text strings.

This module has no network or framework dependencies and performs no logging.
The streaming processor buffers incomplete values across token boundaries
before calling this complete-string detector.
"""

import re


REDACTION = "[REDACTED]"

_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])"
    r"[A-Za-z0-9][A-Za-z0-9._%+-]*@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*"
    r"\.[A-Za-z]{2,}"
    r"(?![A-Za-z0-9_%+-])"
)
_SSN = re.compile(r"(?<![A-Za-z0-9])[0-9]{3}-[0-9]{2}-[0-9]{4}(?![A-Za-z0-9])")
_CARD_CANDIDATE = re.compile(r"(?<![A-Za-z0-9])(?:[0-9][ -]?){12,18}[0-9](?![A-Za-z0-9])")
_MAILTO_LINK = re.compile(r"\[[^\]\r\n]*\]\(mailto:(?P<address>[^)\s]+)\)", re.IGNORECASE)


def _luhn_valid(value: str) -> bool:
    """Check a card candidate without allocating a separate digit list."""
    total = 0
    digit_count = 0
    for character in reversed(value):
        if character in " -":
            continue
        digit_count += 1
        digit = ord(character) - ord("0")
        if digit_count % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return 13 <= digit_count <= 19 and total % 10 == 0


def find_pii_spans(text: str) -> list[tuple[int, int]]:
    """Return sorted, merged spans for detected PII in a complete string.

    The input must be ``str``; other types raise ``TypeError``. Overlapping
    patterns are merged so a value is replaced only once.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a str")
    if not text:
        return []

    spans: list[tuple[int, int]] = []
    for match in _MAILTO_LINK.finditer(text):
        if _EMAIL.fullmatch(match.group("address")):
            spans.append(match.span())
    spans.extend(match.span() for match in _EMAIL.finditer(text))
    spans.extend(match.span() for match in _SSN.finditer(text))
    spans.extend(
        match.span()
        for match in _CARD_CANDIDATE.finditer(text)
        if _luhn_valid(match.group())
    )
    if not spans:
        return []

    spans.sort()
    merged = [spans[0]]
    for start, end in spans[1:]:
        previous_start, previous_end = merged[-1]
        if start < previous_end:
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def redact_pii(text: str) -> str:
    """Replace detected email, formatted SSN, and Luhn-valid card values."""
    spans = find_pii_spans(text)
    if not spans:
        return text

    pieces: list[str] = []
    cursor = 0
    for start, end in spans:
        pieces.append(text[cursor:start])
        pieces.append(REDACTION)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)
