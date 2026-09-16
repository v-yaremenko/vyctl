"""_VyctlScreen carries faint (SGR 2) in pyte's unused blink slot.

Claude Code uses faint heavily for secondary text.  pyte drops SGR 2 entirely, so the
screen subclass remaps it -- and must not corrupt truecolor, where the 2 in
``38;2;r;g;b`` means "truecolor" rather than "faint".
"""

import pytest

pyte = pytest.importorskip("pyte")

from vyctl import terminal as T


@pytest.fixture
def screen():
    s = T._VyctlScreen(40, 5, history=50)
    return s, pyte.Stream(s)


def cell(screen, x=0, y=0):
    return screen.buffer[y][x]


def test_faint_is_carried_as_blink(screen):
    s, stream = screen
    stream.feed("\x1b[2mX")
    assert cell(s).blink is True, "SGR 2 should land in the blink slot"


def test_sgr_22_clears_both_bold_and_faint(screen):
    s, stream = screen
    stream.feed("\x1b[1;2m\x1b[22mX")
    c = cell(s)
    assert c.bold is False
    assert c.blink is False


def test_truecolor_is_not_mistaken_for_faint(screen):
    """The regression guard: 38;2;r;g;b must keep its colour, not become dim."""
    s, stream = screen
    stream.feed("\x1b[38;2;255;100;50mX")
    c = cell(s)
    assert c.blink is False, "the 2 in 38;2;... is truecolor, not SGR 2 faint"
    assert c.fg not in (None, "default")


def test_256_colour_survives(screen):
    s, stream = screen
    stream.feed("\x1b[38;5;196mX")
    assert cell(s).blink is False


def test_bracketed_paste_mode_is_tracked(screen):
    s, stream = screen
    assert s.bracketed_paste is False
    stream.feed("\x1b[?2004h")
    assert s.bracketed_paste is True
    stream.feed("\x1b[?2004l")
    assert s.bracketed_paste is False


def test_lines_scrolled_off_counts_monotonically(screen):
    """The counter that keeps a scrolled-back view anchored to its content."""
    s, stream = screen
    assert s.lines_scrolled_off == 0
    for i in range(20):
        stream.feed(f"row {i}\r\n")
    assert s.lines_scrolled_off == 20 - 5 + 1
