"""Every keystroke typed into a pane goes through encode_key."""

import pytest

from vyctl.terminal import encode_key


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("enter", "\r"),
        ("tab", "\t"),
        ("shift+tab", "\x1b[Z"),
        ("escape", "\x1b"),
        ("backspace", "\x7f"),
        ("up", "\x1b[A"),
        ("down", "\x1b[B"),
        ("left", "\x1b[D"),
        ("right", "\x1b[C"),
    ],
)
def test_named_keys(key, expected):
    assert encode_key(key, None) == expected


@pytest.mark.parametrize(("key", "code"), [("ctrl+c", "\x03"), ("ctrl+a", "\x01"), ("ctrl+z", "\x1a")])
def test_ctrl_letter_becomes_control_code(key, code):
    assert encode_key(key, None) == code


def test_alt_letter_is_escape_prefixed():
    assert encode_key("alt+b", None) == "\x1bb"


def test_printable_passes_through():
    assert encode_key("a", "a") == "a"
    assert encode_key("space", " ") == " "


def test_unknown_key_is_left_to_textual():
    """None means 'not consumed', which is what keeps the app's own bindings working."""
    assert encode_key("f5", None) is None
    assert encode_key("ctrl+shift+alt+q", None) is None


def test_non_printable_character_is_not_forwarded():
    assert encode_key("weird", "\x00") is None
