"""Finding text across a session's scrollback, and jumping to it."""

import pytest

pyte = pytest.importorskip("pyte")

from vyctl import terminal as T
from vyctl.config import AppConfig, Project


@pytest.fixture
def session():
    sess = T.TerminalSession(
        Project(name="p", path="."), AppConfig(), lambda *a, **k: None, rows=6, cols=40
    )
    sess._screen = T._VyctlScreen(40, 6, history=500)
    sess._stream = pyte.Stream(sess._screen)
    for i in range(1, 61):
        text = "the rubric lives here" if i == 12 else f"line {i:03d}"
        sess._stream.feed(f"{text}\r\n")
    return sess


def test_finds_a_line_that_scrolled_off(session):
    hits = session.search("rubric")
    assert len(hits) == 1
    index, text = hits[0]
    assert "rubric" in text
    assert index < session._history_lines, "the match is in history, not on screen"


def test_search_is_case_insensitive(session):
    assert session.search("RUBRIC") == session.search("rubric")


def test_empty_query_finds_nothing(session):
    assert session.search("") == []


def test_missing_text_finds_nothing(session):
    assert session.search("no such string anywhere") == []


def test_multiple_hits_are_ordered_oldest_first(session):
    hits = session.search("line 0")
    assert hits == sorted(hits), "results must read oldest to newest"


def test_scroll_to_line_puts_the_match_on_the_top_row(session):
    index, _ = session.search("rubric")[0]
    session.scroll_to_line(index)
    assert session.visible_text().splitlines()[0].startswith("the rubric")


def test_scrollback_includes_both_history_and_live_screen(session):
    lines = session.scrollback_lines()
    assert "line 060" in lines, "the live screen must be included"
    assert any("rubric" in line for line in lines), "history must be included"


def test_scroll_to_live_tail_index_returns_to_live(session):
    session.scroll_to_line(len(session.scrollback_lines()) - 1)
    assert session.scrolled_back == 0
