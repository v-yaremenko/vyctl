"""Scrollback must survive output arriving while the user is reading history.

pyte's HistoryScreen returns to the bottom on every event that is not a page turn, so
before the session took ownership of the scroll position a running Claude session made
history unreachable: the view snapped to live before the next frame was drawn.
"""

import pytest

pyte = pytest.importorskip("pyte")

from vyctl import terminal as T
from vyctl.config import AppConfig, Project


@pytest.fixture
def session():
    """A TerminalSession wired to an emulator but no real ConPTY."""
    sess = T.TerminalSession(
        Project(name="t", path="."), AppConfig(), lambda *a, **k: None, rows=10, cols=40
    )
    sess._screen = T._VyctlScreen(40, 10, history=500)
    sess._stream = pyte.Stream(sess._screen)
    return sess


def feed_lines(session, start, end):
    for i in range(start, end):
        session._stream.feed(f"line {i:03d}\r\n")


def top_row(session):
    return session.visible_text().splitlines()[0]


def test_starts_live(session):
    feed_lines(session, 1, 101)
    assert session.scrolled_back == 0
    assert session.visible_text().splitlines()[-1] == "line 100"


def test_scroll_up_reaches_history(session):
    feed_lines(session, 1, 101)
    assert session.scroll_up() is True
    assert session.scrolled_back > 0
    assert top_row(session) != "line 091"  # moved off the live window


def test_output_does_not_steal_the_view(session):
    """The regression this module exists for."""
    feed_lines(session, 1, 101)
    session.scroll_up()
    anchored = top_row(session)
    offset = session.scrolled_back

    feed_lines(session, 101, 141)

    assert top_row(session) == anchored, "content moved while scrolled back"
    # The offset counts back from the live bottom, and that bottom moved, so holding
    # the same text means the offset must have grown by the lines that scrolled off.
    assert session.scrolled_back == offset + 40


def test_scroll_to_live_returns_to_tail(session):
    feed_lines(session, 1, 141)
    session.scroll_up()
    session.scroll_to_live()
    assert session.scrolled_back == 0
    assert "line 140" in session.visible_text()


def test_scrolling_past_the_top_clamps(session):
    feed_lines(session, 1, 101)
    for _ in range(500):
        session.scroll_up()
    assert session.scrolled_back == session._history_lines
    assert len(session.visible_text().splitlines()) <= 10


def test_typing_returns_to_live(session):
    """Without this you could type into a prompt scrolled off-screen."""
    feed_lines(session, 1, 101)
    session._pty = type("FakePTY", (), {"write": lambda self, d: len(d)})()
    session.scroll_up()
    assert session.scrolled_back > 0
    session.send_key("a", "a")
    assert session.scrolled_back == 0


def test_cursor_hidden_while_scrolled_back(session):
    """The cursor belongs to the live tail; drawing it over history is wrong."""
    feed_lines(session, 1, 101)
    session.set_focused(True)
    session._screen.cursor.hidden = False
    live = session.render()
    session.scroll_up()
    back = session.render()
    assert live.plain != back.plain
