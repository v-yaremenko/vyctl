"""Status is scraped from what Claude Code draws -- a real TUI emits no events.

These tests pin the strings vyctl depends on.  If Claude Code restyles its footer, the
status dot goes wrong silently in the app; here it goes red loudly instead.
"""

import pytest

pyte = pytest.importorskip("pyte")

from vyctl import terminal as T
from vyctl.config import AppConfig, Project


@pytest.fixture
def session():
    sess = T.TerminalSession(
        Project(name="t", path="."), AppConfig(), lambda *a, **k: None, rows=8, cols=60
    )
    sess._screen = T._VyctlScreen(60, 8, history=50)
    sess._stream = pyte.Stream(sess._screen)
    return sess


def test_idle_by_default(session):
    session._stream.feed("just some output\r\n")
    session._refresh_status()
    assert session.status is T.TerminalStatus.IDLE


@pytest.mark.parametrize("marker", T._BUSY_MARKERS)
def test_each_busy_marker_is_detected(session, marker):
    session._stream.feed(f"Thinking... ({marker})\r\n")
    session._refresh_status()
    assert session.status is T.TerminalStatus.WORKING


def test_busy_detection_is_case_insensitive(session):
    session._stream.feed("ESC TO INTERRUPT\r\n")
    session._refresh_status()
    assert session.status is T.TerminalStatus.WORKING


def test_returns_to_idle_when_the_marker_clears(session):
    session._stream.feed("esc to interrupt\r\n")
    session._refresh_status()
    assert session.status is T.TerminalStatus.WORKING

    session._stream.feed("\x1b[2J\x1b[H")  # clear screen
    session._refresh_status()
    assert session.status is T.TerminalStatus.IDLE


@pytest.mark.parametrize("marker", T._PROMPT_MARKERS)
def test_awaiting_input_markers(session, marker):
    """These drive the 'this session needs you' signal."""
    session._stream.feed(f"{marker}\r\n")
    assert session.awaiting_input() is True


def test_not_awaiting_input_on_ordinary_output(session):
    session._stream.feed("all done, no questions here\r\n")
    assert session.awaiting_input() is False
