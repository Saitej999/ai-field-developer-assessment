"""Complete-string PII detector checks using synthetic examples only."""

import logging

import pytest

from task3_streaming_guardrails.pii_detector import find_pii_spans, redact_pii


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("Email: john.doe+test@example.com.", "Email: [REDACTED]."),
        ("SSN: 123-45-6789.", "SSN: [REDACTED]."),
        ("Card: 4111111111111111.", "Card: [REDACTED]."),
        ("Card: 4111 1111 1111 1111.", "Card: [REDACTED]."),
        ("Card: 5555-5555-5555-4444.", "Card: [REDACTED]."),
        ("13-digit card: 4222222222222.", "13-digit card: [REDACTED]."),
        ("15-digit card: 378282246310005.", "15-digit card: [REDACTED]."),
        ("19-digit card: 4000000000000000006.", "19-digit card: [REDACTED]."),
    ],
)
def test_each_supported_pii_pattern(source, expected):
    assert redact_pii(source) == expected


def test_multiple_pii_values_and_surrounding_text():
    source = "Contact jane@example.org, SSN 123-45-6789, or card 4111 1111 1111 1111; keep this note."
    assert redact_pii(source) == "Contact [REDACTED], SSN [REDACTED], or card [REDACTED]; keep this note."


def test_multiple_email_addresses():
    assert redact_pii("a@example.com and b@example.net") == "[REDACTED] and [REDACTED]"


@pytest.mark.parametrize("text", ["", "No sensitive details here.", "The code is 1234."])
def test_text_without_pii_is_unchanged(text):
    assert redact_pii(text) == text
    assert find_pii_spans(text) == []


def test_existing_placeholder_is_preserved():
    assert redact_pii("Already [REDACTED]; new contact: jane@example.com") == "Already [REDACTED]; new contact: [REDACTED]"


def test_overlapping_mailto_link_is_replaced_once():
    source = "My email is [john@gmail.com](mailto:john@gmail.com) and my SSN is 123-45-6789."
    assert redact_pii(source) == "My email is [REDACTED] and my SSN is [REDACTED]."
    spans = find_pii_spans(source)
    assert [source[start:end] for start, end in spans] == [
        "[john@gmail.com](mailto:john@gmail.com)",
        "123-45-6789",
    ]


def test_non_luhn_card_like_number_is_not_redacted():
    assert redact_pii("Reference 4111-1111-1111-1112 is invalid.") == "Reference 4111-1111-1111-1112 is invalid."


def test_non_ascii_numerals_are_not_treated_as_us_ssn_or_card_digits():
    text = "Examples: ١٢٣-٤٥-٦٧٨٩ and ٤١١١١١١١١١١١١١١١."
    assert redact_pii(text) == text


@pytest.mark.parametrize("value", [None, 123, b"john@example.com", ["john@example.com"]])
def test_non_string_inputs_raise_type_error(value):
    with pytest.raises(TypeError, match="text must be a str"):
        redact_pii(value)
    with pytest.raises(TypeError, match="text must be a str"):
        find_pii_spans(value)


def test_sensitive_values_are_not_logged_or_printed(caplog, capsys):
    source = "jane@example.com 123-45-6789 4111111111111111"
    with caplog.at_level(logging.DEBUG):
        assert redact_pii(source) == "[REDACTED] [REDACTED] [REDACTED]"
    captured = capsys.readouterr()
    assert caplog.text == ""
    assert captured.out == "" and captured.err == ""
