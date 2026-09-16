"""The search dialog itself, driven through Textual's test harness."""

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Label, ListView, Static

from vyctl.widgets import SearchDialog

HITS = [
    ("p1", "CS101", 12, "the rubric lives here"),
    ("p2", "OOP_asmt1", 40, "rubric again"),
]


def fake_search(query: str):
    return [h for h in HITS if query.lower() in h[3].lower()] if query else []


class Host(App[None]):
    def compose(self) -> ComposeResult:
        yield Label("host")


@pytest.mark.asyncio
async def test_typing_renders_matches():
    app = Host()
    async with app.run_test() as pilot:
        app.push_screen(SearchDialog(fake_search))
        await pilot.pause()
        app.screen.query_one("#search-query").value = "rubric"
        await pilot.pause()
        results = app.screen.query_one("#search-results", ListView)
        assert len(results.children) == 2
        assert "2 matches" in str(app.screen.query_one("#search-count", Static).render())


@pytest.mark.asyncio
async def test_no_matches_is_reported():
    app = Host()
    async with app.run_test() as pilot:
        app.push_screen(SearchDialog(fake_search))
        await pilot.pause()
        app.screen.query_one("#search-query").value = "nothing here"
        await pilot.pause()
        assert "no matches" in str(app.screen.query_one("#search-count", Static).render())


@pytest.mark.asyncio
async def test_enter_jumps_to_the_first_hit():
    app = Host()
    result = {}
    async with app.run_test() as pilot:
        app.push_screen(SearchDialog(fake_search), lambda r: result.update(hit=r))
        await pilot.pause()
        app.screen.query_one("#search-query").value = "rubric"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert result["hit"] == ("p1", 12)


@pytest.mark.asyncio
async def test_escape_cancels():
    app = Host()
    result = {}
    async with app.run_test() as pilot:
        app.push_screen(SearchDialog(fake_search), lambda r: result.update(hit=r))
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert result["hit"] is None
