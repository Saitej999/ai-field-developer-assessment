"""Bounded, fail-closed redaction of text arriving in arbitrary chunks.

Only the current email-like or numeric candidate is retained. Candidates are
not emitted until a boundary establishes their full value. An oversized
candidate is replaced once, then discarded through its next boundary.
"""

from string import ascii_letters, digits

from task3_streaming_guardrails.pii_detector import REDACTION, redact_pii


MAX_EMAIL_CANDIDATE = 256
MAX_NUMERIC_CANDIDATE = 64

_EMAIL_CHARS = frozenset(ascii_letters + digits + "._%+-@")
_NUMERIC_CHARS = frozenset(digits + " -")


def _incomplete_numeric(candidate: str) -> bool:
    """Recognize card/SSN-shaped prefixes that must not be released raw."""
    digits_only = sum(character in digits for character in candidate)
    if digits_only >= 8:
        # Includes incomplete contiguous card values and invalid checksums.
        return True
    if len(candidate) >= 4 and candidate[:3].isdigit() and candidate[3] == "-":
        return True
    if len(candidate) >= 5 and candidate[:4].isdigit() and candidate[4] in " -":
        return True
    return False


class StreamingPIIRedactor:
    """Sanitize complete candidate runs while releasing safe text promptly.

    ``feed`` returns only safe text produced by the new chunk. ``finish``
    resolves the remaining candidate and closes the processor. Input must be
    ``str``. The retained candidate never exceeds the documented limits.
    """

    def __init__(self) -> None:
        self._mode: str | None = None
        self._candidate: list[str] = []
        self._suppressed_tail_space = False
        self._closed = False
        self.max_buffered_chars = 0

    @property
    def buffered_chars(self) -> int:
        return len(self._candidate)

    def _append(self, character: str, output: list[str]) -> None:
        limit = MAX_NUMERIC_CANDIDATE if self._mode == "numeric" else MAX_EMAIL_CANDIDATE
        if len(self._candidate) >= limit:
            output.append(REDACTION)
            self._suppressed_tail_space = character == " "
            self._candidate.clear()
            self._mode = "suppressed_numeric" if self._mode == "numeric" else "suppressed_email"
            return
        self._candidate.append(character)
        self.max_buffered_chars = max(self.max_buffered_chars, len(self._candidate))

    def _finish_candidate(self, output: list[str]) -> None:
        candidate = "".join(self._candidate)
        self._candidate.clear()
        if self._mode == "numeric":
            core = candidate.rstrip(" -")
            suffix = candidate[len(core):]
            sanitized = redact_pii(core)
            if sanitized == core and _incomplete_numeric(core):
                sanitized = REDACTION
                suffix = suffix.replace("-", "")
            output.append(sanitized + suffix)
        elif self._mode == "email":
            sanitized = redact_pii(candidate)
            # An @ that remains after complete-string detection is still an
            # identifiable incomplete or malformed email candidate.
            output.append(
                REDACTION
                if "@" in sanitized or (sanitized == candidate and _incomplete_numeric(candidate))
                else sanitized
            )
        self._mode = None

    def feed(self, chunk: str) -> str:
        if not isinstance(chunk, str):
            raise TypeError("chunk must be a str")
        if self._closed:
            raise RuntimeError("processor is already finished")
        if not chunk:
            return ""

        output: list[str] = []
        for character in chunk:
            while True:
                if self._mode == "suppressed_email":
                    if character in _EMAIL_CHARS:
                        break
                    self._mode = None
                elif self._mode == "suppressed_numeric":
                    if character in _NUMERIC_CHARS:
                        self._suppressed_tail_space = character == " "
                        break
                    if self._suppressed_tail_space:
                        output.append(" ")
                    self._suppressed_tail_space = False
                    self._mode = None
                elif self._mode == "email":
                    if character in _EMAIL_CHARS:
                        self._append(character, output)
                        break
                    self._finish_candidate(output)
                elif self._mode == "numeric":
                    if character in _NUMERIC_CHARS:
                        self._append(character, output)
                        break
                    if character in _EMAIL_CHARS and " " not in self._candidate:
                        # A numeric prefix can become an email local part.
                        self._mode = "email"
                        self._append(character, output)
                        break
                    self._finish_candidate(output)
                else:
                    if character in digits:
                        self._mode = "numeric"
                        self._append(character, output)
                    elif character in _EMAIL_CHARS:
                        self._mode = "email"
                        self._append(character, output)
                    else:
                        output.append(character)
                    break
        return "".join(output)

    def finish(self) -> str:
        if self._closed:
            raise RuntimeError("processor is already finished")
        self._closed = True
        output: list[str] = []
        if self._mode in {"email", "numeric"}:
            self._finish_candidate(output)
        elif self._mode == "suppressed_numeric" and self._suppressed_tail_space:
            output.append(" ")
        self._mode = None
        return "".join(output)
