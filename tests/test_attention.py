"""A background session that finishes, or asks a question, must flag itself.

Running several sessions only pays off if you are not watching them, so the signal
has to come to you.
"""

import pytest

pyte = pytest.importorskip("pyte")

from vyctl import terminal as T
from vyctl.config import AppConfig, Project


def make_session(**config_kwargs):
    cfg = AppConfig(**config_kwargs)
    sess = T.TerminalSession(
        Project(name="proj", path="."), cfg, lambda *a, **k: None, rows=8, cols=60
    )
    sess._screen = T._VyctlScreen(60, 8, history=50)
    sess._stream = pyte.Stream(sess._screen)
    return sess


def work_then_finish(session):
    session._stream.feed("thinking... esc to interrupt\r\n")
    session._refresh_status()
    session._stream.feed("\x1b[2J\x1b[H")  # turn over, footer gone
    session._refresh_status()


def test_finishing_a_turn_raises_attention():
    s = make_session()
    assert s.attention is False
    work_then_finish(s)
    assert s.attention is True
    assert "finished" in s.attention_reason


def test_no_attention_without_having_been_busy():
    """Plain output must not nag -- only a turn that actually ended."""
    s = make_session()
    s._stream.feed("just some text\r\n")
    s._refresh_status()
    assert s.attention is False


def test_permission_prompt_raises_attention():
    s = make_session()
    s._stream.feed("Do you want to proceed?\r\n")
    s._refresh_status()
    assert s.attention is True
    assert "approval" in s.attention_reason


def test_standing_prompt_raises_once():
    """The prompt stays on screen; it must not re-fire on every repaint."""
    s = make_session()
    s._stream.feed("Do you want to proceed?\r\n")
    s._refresh_status()
    s.clear_attention()
    s._refresh_status()
    s._refresh_status()
    assert s.attention is False, "a standing prompt re-raised attention"


def test_focused_session_does_not_nag():
    """If the user is already looking at it, there is nothing to announce."""
    s = make_session()
    s.set_focused(True)
    work_then_finish(s)
    assert s.attention is False


def test_disabled_by_config():
    s = make_session(notify_on_idle=False)
    work_then_finish(s)
    assert s.attention is False


def test_clear_attention_is_idempotent():
    s = make_session()
    work_then_finish(s)
    s.clear_attention()
    assert s.attention is False and s.attention_reason == ""
    s.clear_attention()
    assert s.attention is False
