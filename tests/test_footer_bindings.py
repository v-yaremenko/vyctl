"""The footer is the only discoverable list of keys, so what it shows matters."""

from vyctl.app import VyctlApp

SHOWN = {b.key: b.description for b in VyctlApp.BINDINGS if getattr(b, "show", True)}


def test_search_is_offered_in_the_footer():
    assert SHOWN.get("f6") == "Search"


def test_the_footer_keeps_the_core_four():
    """Adding to the footer must not crowd out what was already there."""
    for key, label in (("f1", "Console"), ("f2", "Projects"), ("f3", "Tasks"), ("f4", "Help")):
        assert SHOWN.get(key) == label


def test_quit_stays_visible():
    assert SHOWN.get("ctrl+q") == "Quit"


def test_the_footer_stays_short():
    """Textual adds its own ctrl+p, so the rendered footer is one longer than this.

    Seven entries were measured to fit an 80-column window with nothing clipped; the
    cap is here so the next addition is a deliberate decision rather than a surprise.
    """
    assert len(SHOWN) <= 6, f"footer has grown to {len(SHOWN)}: {sorted(SHOWN)}"


def test_secondary_keys_stay_hidden():
    """f5/f8/f9/f10/f12 are documented in f4 help, not crammed into the footer."""
    for key in ("f5", "f8", "f9", "f10", "f12"):
        assert key not in SHOWN
