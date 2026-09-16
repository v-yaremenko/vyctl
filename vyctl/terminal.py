"""
Real interactive terminal sessions for vyctl ("interactive" pane mode).

Where :mod:`vyctl.process` drives Claude headlessly through pipes, this module gives
each project a genuine **pseudo-console**: a ``powershell.exe`` attached to a Windows
ConPTY, inside which the ordinary interactive ``claude`` TUI runs exactly as it does in a
normal terminal window -- same prompt box, same spinners, same slash commands, same
keyboard shortcuts.

Three pieces make that work:

* **pywinpty** creates the ConPTY and gives us non-blocking reads of everything the
  console writes, plus a ``set_size`` that makes Claude Code reflow live.
* **pyte** is a terminal emulator: we feed it that byte stream and it maintains the
  screen contents (characters + colours + cursor) the way a real terminal would.
* a small key encoder turns Textual key events back into the escape sequences a console
  application expects, so typing in a pane lands in the real Claude prompt.

Both dependencies are optional: if either is missing, :data:`AVAILABLE` is False and the
app falls back to headless panes.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from collections.abc import Callable
from enum import Enum
from functools import lru_cache
from itertools import islice
from pathlib import Path

from rich.style import Style
from rich.text import Text

from . import logs
from .config import AppConfig, Project
from .process import _LIVE_PIDS, ProcessJob, kill_process_tree, ps_quote

try:  # pragma: no cover - import guard is environment specific
    import pyte
    from winpty import PTY

    AVAILABLE = True
    IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover
    AVAILABLE = False
    IMPORT_ERROR = str(exc)
    pyte = None  # type: ignore[assignment]
    PTY = None  # type: ignore[assignment]


# --------------------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------------------


class TerminalStatus(str, Enum):
    """Pane badge state for an interactive session."""

    INACTIVE = "inactive"
    STARTING = "starting"
    IDLE = "idle"
    WORKING = "working"
    ERROR = "error"

    @property
    def label(self) -> str:
        return {
            TerminalStatus.INACTIVE: "Inactive",
            TerminalStatus.STARTING: "Starting",
            TerminalStatus.IDLE: "Idle",
            TerminalStatus.WORKING: "Thinking/Working",
            TerminalStatus.ERROR: "Error",
        }[self]


#: Claude Code prints this hint while a turn is running; it is the most reliable signal
#: that the session is busy, since in a real TUI there is no machine-readable event.
_BUSY_MARKERS = (
    "esc to interrupt",
    "interrupt)",
)

#: Text shown while Claude is waiting for the user to approve a tool call.
_PROMPT_MARKERS = (
    "do you want to",
    "yes, and don't ask again",
    "enter to confirm",
    "1. yes",
)


# --------------------------------------------------------------------------------------
# Key encoding
# --------------------------------------------------------------------------------------

#: Textual key name -> bytes a console application expects.
_KEY_SEQUENCES: dict[str, str] = {
    "enter": "\r",
    "tab": "\t",
    "shift+tab": "\x1b[Z",
    "escape": "\x1b",
    "backspace": "\x7f",
    "delete": "\x1b[3~",
    "insert": "\x1b[2~",
    "up": "\x1b[A",
    "down": "\x1b[B",
    "right": "\x1b[C",
    "left": "\x1b[D",
    "home": "\x1b[H",
    "end": "\x1b[F",
    "pageup": "\x1b[5~",
    "pagedown": "\x1b[6~",
    "shift+enter": "\x1b\r",
    "alt+enter": "\x1b\r",
    "ctrl+enter": "\x1b\r",
    "shift+up": "\x1b[1;2A",
    "shift+down": "\x1b[1;2B",
    "shift+right": "\x1b[1;2C",
    "shift+left": "\x1b[1;2D",
    "ctrl+right": "\x1b[1;5C",
    "ctrl+left": "\x1b[1;5D",
    "space": " ",
    # Deliberately absent: f1/f2/f3 (tab switching) and f12 (release the keyboard) are
    # reserved by vyctl, since Claude Code itself does not use function keys.
}


def encode_key(key: str, character: str | None) -> str | None:
    """Translate a Textual key event into the sequence a console app expects.

    Returns ``None`` when the key should be left to Textual (so the app's own bindings
    keep working even while a terminal pane holds focus).
    """
    if key in _KEY_SEQUENCES:
        return _KEY_SEQUENCES[key]

    # ctrl+<letter> -> the corresponding control character (ctrl+c -> 0x03).
    if key.startswith("ctrl+") and len(key) == 6:
        letter = key[5]
        if "a" <= letter <= "z":
            return chr(ord(letter) - 96)

    # alt+<letter> -> ESC <letter>
    if key.startswith("alt+") and len(key) == 5:
        return "\x1b" + key[4]

    # Anything that produced a printable character goes through as-is.
    if character and character.isprintable():
        return character
    return None


# --------------------------------------------------------------------------------------
# pyte -> Rich translation
# --------------------------------------------------------------------------------------

#: pyte's colour names that Rich spells differently.
_COLOR_ALIASES = {
    "brown": "yellow",
    "brightblack": "bright_black",
    "brightred": "bright_red",
    "brightgreen": "bright_green",
    "brightbrown": "bright_yellow",
    "brightyellow": "bright_yellow",
    "brightblue": "bright_blue",
    "brightmagenta": "bright_magenta",
    "brightcyan": "bright_cyan",
    "brightwhite": "bright_white",
}

_HEX_RE = re.compile(r"\A[0-9a-fA-F]{6}\Z")

#: PowerShell's default prompt -- the cue that the shell is ready for typed input.
_PS_PROMPT_RE = re.compile(r"PS\s+\S.*>\s*\Z")

#: Environment markers Claude Code sets for the processes it spawns.  If vyctl is
#: itself launched from inside a Claude Code session, the embedded sessions would inherit
#: them and behave like nested child sessions -- which disables transcript saving, and
#: therefore breaks ``--continue``/auto-resume.  Each pane should look like a fresh
#: terminal, so these are dropped from the console's environment.
_STRIPPED_ENV = frozenset(
    {
        "CLAUDECODE",
        "CLAUDE_CODE_CHILD_SESSION",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SSE_PORT",
        "CLAUDE_CODE_SESSION_ID",
    }
)

#: CSI sequences carrying a **private parameter prefix** (``<``, ``=``, ``>``), which pyte
#: does not understand -- it strips the prefix and parses the rest as if it were standard.
#:
#: This is not cosmetic.  Claude Code emits ``CSI > 4 ; 2 m`` (xterm's "modifyOtherKeys",
#: a keyboard-protocol negotiation) on every repaint.  pyte reads that as a plain SGR with
#: parameters 4 and 2, i.e. "underline on" + "faint on", and those stick to the cursor's
#: attributes -- so every character drawn afterwards, including the user's own typed input,
#: came out underlined and dim.  The kitty keyboard sequences (``CSI > 1 u`` / ``CSI < u``)
#: are the same story and additionally spilled a stray "u" onto the screen.
#:
#: Only the ``<``/``=``/``>`` prefixes are dropped, plus ``CSI ? … u``.  ``CSI ? … h/l``
#: (DEC private modes such as ``?25l`` hide-cursor and ``?2026h`` synchronised output) is
#: deliberately left intact, because pyte does model those.
_UNSUPPORTED_CSI_RE = re.compile(r"\x1b\[(?:[<=>][0-9;:]*[A-Za-z]|\?[0-9;:]*u)")


if AVAILABLE:  # pragma: no branch - only meaningful when pyte imported

    class _VyctlScreen(pyte.HistoryScreen):
        """A pyte screen with scrollback that also tracks SGR 2 (faint/dim).

        ``HistoryScreen`` (rather than plain ``Screen``) is what makes the pane
        scrollable: a plain screen keeps only the visible rows, so there is literally
        nothing above the top line to scroll to.

        pyte pages the screen itself via ``prev_page``/``next_page``, and
        ``before_event`` snaps back to the bottom on every event that is not one of
        those two.  While Claude Code is typing, output never stops, so that snap-back
        fired continuously and scrolling back was impossible -- the view jumped to live
        before the next frame was drawn.  We therefore never page pyte at all: the
        screen is always live, and :class:`TerminalSession` keeps its own offset and
        composes the visible window from ``history.top`` plus the buffer.  Overriding
        ``before_event`` away is what keeps pyte's paging from fighting that.

        pyte models bold, italics, underline, reverse, blink and strike, but it has no
        concept of *faint* -- SGR 2 is simply dropped.  Claude Code uses it heavily for
        secondary text (the ``Try "..."`` hint, mode footers, timestamps), so all of that
        arrived as plain white instead of dim.

        Rather than fork pyte, dim is carried in the ``blink`` slot: Claude Code never
        blinks, so the field is free, and :func:`_style_for` renders it as dim.  SGR 22
        ("normal intensity") has to clear both bold and faint, which pyte would otherwise
        apply to bold alone.
        """

        #: SGR codes that introduce an extended colour and consume following params:
        #: ``38;5;n`` / ``48;2;r;g;b`` / ``58;…``.  Their arguments must be copied
        #: verbatim -- the ``2`` in ``38;2;r;g;b`` means "truecolor", not "faint", and
        #: rewriting it would turn the colour into a 256-colour lookup and leave r/g/b
        #: behind as stray attribute codes.
        _EXTENDED_COLOUR = (38, 48, 58)

        #: True once the application enables bracketed paste (DEC private mode 2004).
        #: Claude Code then receives a pasted block as one unit instead of treating
        #: every newline in it as a separate Enter.
        bracketed_paste = False

        #: How many lines have scrolled off the top since the screen was created.
        #: Monotonic -- unlike ``len(history.top)`` it keeps counting once the deque
        #: saturates, which is what lets a scrolled-back view stay on its content.
        lines_scrolled_off = 0

        def index(self) -> None:
            """Overloaded purely to count lines pushed into the history deque."""
            bottom_margin = self.margins[1] if self.margins else self.lines - 1
            scrolling = self.cursor.y == bottom_margin
            super().index()
            if scrolling:
                self.lines_scrolled_off += 1

        def before_event(self, event: str) -> None:
            """Never auto-scroll to the bottom; the session owns the scroll position.

            pyte's implementation pages back to live for every event other than
            ``prev_page``/``next_page``.  We never call either, so the screen stays at
            the bottom of its own history regardless -- but leaving the hook active
            would undo any paging that ever did happen, so it goes.
            """

        def set_mode(self, *modes: int, **kwargs) -> None:
            if kwargs.get("private") and 2004 in modes:
                self.bracketed_paste = True
            super().set_mode(*modes, **kwargs)

        def reset_mode(self, *modes: int, **kwargs) -> None:
            if kwargs.get("private") and 2004 in modes:
                self.bracketed_paste = False
            super().reset_mode(*modes, **kwargs)

        def select_graphic_rendition(self, *attrs: int) -> None:
            mapped: list[int] = []
            index = 0
            count = len(attrs)
            while index < count:
                attr = attrs[index]

                if attr in self._EXTENDED_COLOUR:
                    mapped.append(attr)
                    index += 1
                    if index < count:
                        mode = attrs[index]
                        mapped.append(mode)
                        index += 1
                        # 5 -> one palette index, 2 -> three colour channels.
                        arguments = 1 if mode == 5 else 3 if mode == 2 else 0
                        for _ in range(arguments):
                            if index >= count:
                                break
                            mapped.append(attrs[index])
                            index += 1
                    continue

                if attr == 2:  # faint -> the blink slot
                    mapped.append(5)
                elif attr == 22:  # normal intensity clears bold AND faint
                    mapped.extend((22, 25))
                else:
                    mapped.append(attr)
                index += 1

            super().select_graphic_rendition(*mapped)

else:  # pragma: no cover
    _VyctlScreen = None  # type: ignore[assignment]


#: Settings file vyctl hands to the Claude sessions it embeds.
_CONSOLE_SETTINGS_NAME = "console-settings.json"

#: Pins Claude Code's *classic* renderer for embedded consoles.  Its fullscreen renderer
#: drives the alternate screen buffer, which pyte does not model -- the symptom is a pane
#: where everything looks highlighted.  This is passed per launch via ``--settings`` so
#: the user's own ``~/.claude/settings.json`` (which may well say ``"tui": "fullscreen"``
#: for their normal terminal work) is left completely alone.
_CLASSIC_TUI_SETTINGS = '{\n  "tui": "default"\n}\n'


def _classic_tui_settings_path() -> str | None:
    """Return the path of a settings file pinning the classic renderer.

    Written next to the application, falling back to the temp directory if that is not
    writable.  Returns ``None`` if neither works, in which case the console simply
    launches with the user's own settings.
    """
    for base in (Path(__file__).resolve().parent.parent, Path(tempfile.gettempdir())):
        path = base / _CONSOLE_SETTINGS_NAME
        try:
            if not path.exists() or path.read_text(encoding="utf-8") != _CLASSIC_TUI_SETTINGS:
                path.write_text(_CLASSIC_TUI_SETTINGS, encoding="utf-8")
            return str(path)
        except OSError:
            continue
    return None


def _console_env() -> str | None:
    """Build the ConPTY environment block, minus the nested-session markers.

    pywinpty wants ``name=value`` pairs joined by NUL characters.
    """
    pairs = [f"{k}={v}" for k, v in os.environ.items() if k not in _STRIPPED_ENV]
    if not pairs:
        return None
    return "\0".join(pairs) + "\0"


def _rich_color(value: str | None) -> str | None:
    """Convert one pyte colour token into something Rich understands."""
    if not value or value == "default":
        return None
    if _HEX_RE.match(value):
        return "#" + value.lower()
    return _COLOR_ALIASES.get(value, value)


@lru_cache(maxsize=4096)
def _style_for(
    fg: str,
    bg: str,
    bold: bool,
    italics: bool,
    reverse: bool,
    strike: bool,
    dim: bool,
    underline: bool,
) -> Style:
    """Cached pyte-attributes -> Rich Style.

    The cache matters: a full 100x30 pane is 3000 cells per frame, and without it we
    would rebuild identical Style objects thousands of times a second.

    ``dim`` arrives in pyte's blink slot; see :class:`_VyctlScreen` for why.

    Underline is rendered normally.  It used to be suppressed because whole rows came out
    underlined, but that turned out to be ``CSI > 4 ; 2 m`` being mis-parsed as SGR 4 --
    see :data:`_UNSUPPORTED_CSI_RE`, which removes the cause.
    """
    return Style(
        color=_rich_color(fg),
        bgcolor=_rich_color(bg),
        bold=bold or None,
        italic=italics or None,
        reverse=reverse or None,
        strike=strike or None,
        dim=dim or None,
        underline=underline or None,
    )


# --------------------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------------------

#: ``(session)`` -> None, fired when the screen changed or the status moved.
TerminalCallback = Callable[["TerminalSession"], None]


class TerminalSession:
    """One project's ConPTY, its terminal emulator, and the reader task feeding it."""

    #: Poll interval when the console is quiet.  Reads are cheap, and this keeps a
    #: four-pane dashboard responsive without burning a core on busy-waiting.
    IDLE_POLL = 0.02
    #: Minimum gap between UI repaints (~25 fps) so a chatty build log cannot starve
    #: the rest of the app.
    REPAINT_INTERVAL = 0.04

    def __init__(
        self,
        project: Project,
        config: AppConfig,
        on_change: TerminalCallback,
        rows: int = 24,
        cols: int = 80,
    ) -> None:
        self.project = project
        self.config = config
        self._on_change = on_change
        self.rows = max(rows, 4)
        self.cols = max(cols, 20)

        self._pty = None
        self._job: ProcessJob | None = None
        self._screen = None
        self._stream = None
        self._reader: asyncio.Task | None = None
        self._status = TerminalStatus.INACTIVE
        self._dirty = False
        self._focused = False
        self.pid: int | None = None
        #: Set once the startup command has been written to the console.
        self._launched = False
        #: Loop timestamp of the first byte read, for the launch fallback timer.
        self._first_data_at: float | None = None
        #: Lines above the live view currently on show (0 = live).  Owned here rather
        #: than by pyte so that incoming output cannot yank the view back to the bottom.
        self._scroll_offset = 0
        #: ``lines_scrolled_off`` as of the last time the offset was reconciled.
        self._scroll_mark = 0

    # -- introspection ----------------------------------------------------------

    @property
    def status(self) -> TerminalStatus:
        return self._status

    @property
    def is_running(self) -> bool:
        if self._pty is None:
            return False
        try:
            return bool(self._pty.isalive())
        except Exception:
            return False

    def set_focused(self, focused: bool) -> None:
        """Track focus so the cursor is only drawn in the active pane."""
        if focused != self._focused:
            self._focused = focused
            self._dirty = True
            self._notify()

    def _set_status(self, status: TerminalStatus) -> None:
        if status is not self._status:
            logs.log().info(
                "[%s] status %s -> %s", self.project.name, self._status.value, status.value
            )
            self._status = status
            self._notify()

    def _notify(self) -> None:
        try:
            self._on_change(self)
        except Exception:  # pragma: no cover - a broken pane must not kill the reader
            pass

    # -- lifecycle --------------------------------------------------------------

    def start(self) -> bool:
        """Spawn the console and launch Claude inside it."""
        if self.is_running:
            return True
        if not AVAILABLE:
            return False
        if not self.project.path_exists():
            self._set_status(TerminalStatus.ERROR)
            return False

        self._screen = _VyctlScreen(
            self.cols, self.rows, history=max(self.config.scrollback_lines, self.rows)
        )
        self._stream = pyte.Stream(self._screen)
        self._scroll_offset = 0
        self._scroll_mark = 0
        self._set_status(TerminalStatus.STARTING)

        try:
            self._pty = PTY(self.cols, self.rows)
            args = {
                "cmdline": " -NoLogo -NoProfile -ExecutionPolicy Bypass",
                "cwd": str(self.project.resolved_path),
            }
            try:
                spawned = self._pty.spawn(
                    _powershell_path(), env=_console_env(), **args
                )
            except Exception:
                # A rejected environment block must not cost us the session; inherit
                # the parent environment instead.
                spawned = self._pty.spawn(_powershell_path(), **args)
        except Exception as exc:
            self._write_notice(f"could not open a console: {exc}")
            self._set_status(TerminalStatus.ERROR)
            self._pty = None
            return False

        if not spawned:
            self._write_notice("could not start powershell.exe in a console")
            self._set_status(TerminalStatus.ERROR)
            self._pty = None
            return False

        self.pid = getattr(self._pty, "pid", None)
        if self.pid:
            _LIVE_PIDS.add(self.pid)
            # Same orphan protection as the headless sessions: everything the console
            # spawns (claude, node, anything it detaches) joins this job object.
            self._job = ProcessJob()
            if not self._job.assign(self.pid):
                self._job.close()
                self._job = None

        logs.log().info(
            "[%s] console pid=%s cwd=%s job=%s",
            self.project.name,
            self.pid,
            self.project.resolved_path,
            self._job is not None,
        )
        self._reader = asyncio.get_running_loop().create_task(self._pump())
        return True

    def _startup_command(self) -> str:
        """The command line typed into the fresh console to launch Claude."""
        base = (self.project.command or "claude").strip() or "claude"

        # Flags shared by both the resumed and the fresh invocation.  A file path is
        # passed to --settings rather than inline JSON: Windows PowerShell strips
        # embedded double quotes when forwarding arguments to a native executable, so
        # `--settings '{"tui":"default"}'` would arrive mangled.
        flags: list[str] = []
        if self.config.force_classic_tui:
            settings = _classic_tui_settings_path()
            if settings:
                flags.append(f"--settings {ps_quote(settings)}")
        extra = (self.project.extra_args or "").strip()
        if extra:
            flags.append(extra)

        launch = " ".join([base, *flags])
        if not self.config.auto_resume:
            return launch
        # Auto-resume: interactively, `--continue` reopens this folder's latest
        # conversation.  If there is nothing to resume it exits non-zero, so fall
        # straight through to a fresh session rather than leaving a bare shell.
        resumed = " ".join([base, "--continue", *flags])
        return f"{resumed}; if ($LASTEXITCODE -ne 0) {{ {launch} }}"

    def _ready_for_input(self) -> bool:
        """True once PowerShell has drawn a prompt and can accept typed input.

        Writing to a ConPTY before the shell is reading discards the input, so we wait
        for an actual ``PS ...>`` prompt rather than firing on the first byte.  The
        elapsed-time arm is a fallback for a customised prompt we would not recognise.
        """
        if self._screen is None:
            return False
        if _PS_PROMPT_RE.search(self._screen.display[self._screen.cursor.y]):
            return True
        for line in reversed(self._screen.display):
            if line.strip():
                return bool(_PS_PROMPT_RE.search(line))
        return False

    def _launch_claude(self) -> None:
        """Type the startup command once PowerShell is ready for input."""
        if self._launched:
            return
        self._launched = True
        command = self._startup_command()
        logs.log().info("[%s] launching: %s", self.project.name, command)
        self.write(command + "\r")

    async def stop(self) -> None:
        """Close the console and reap everything inside it."""
        pty, self._pty = self._pty, None
        job, self._job = self._job, None
        pid, self.pid = self.pid, None
        reader, self._reader = self._reader, None
        self._launched = False
        self._set_status(TerminalStatus.INACTIVE)

        logs.log().info("[%s] stopping console pid=%s", self.project.name, pid)
        if reader is not None:
            reader.cancel()

        loop = asyncio.get_running_loop()
        if pty is not None:
            # Ask the console to close, so Claude Code can save its session state.
            try:
                pty.write("\x03")  # ctrl+c: cancel whatever is in flight
                pty.write("exit\r")
            except Exception:
                pass
            await asyncio.sleep(0.25)

        if job is not None:
            await loop.run_in_executor(None, job.terminate)
        if pid:
            await loop.run_in_executor(None, kill_process_tree, pid)
        if pty is not None:
            try:
                del pty  # releases the ConPTY handles
            except Exception:
                pass

    async def restart(self) -> bool:
        await self.stop()
        return self.start()

    # -- input ------------------------------------------------------------------

    def write(self, data: str) -> bool:
        """Send raw text/escape sequences to the console."""
        if self._pty is None or not data:
            return False
        # Typing jumps back to the live view, as every real terminal does.  Output no
        # longer drags the view down by itself, so without this you could type into a
        # prompt you cannot see.  It is a no-op when already live.
        self.scroll_to_live()
        try:
            self._pty.write(data)
            return True
        except Exception:
            self._set_status(TerminalStatus.ERROR)
            return False

    def send_key(self, key: str, character: str | None) -> bool:
        """Forward one Textual key event.  False means "not consumed"."""
        sequence = encode_key(key, character)
        if sequence is None:
            return False
        return self.write(sequence)

    def send_text(self, text: str, submit: bool = True) -> bool:
        """Type *text* into Claude's prompt box and (optionally) press Enter.

        This is how a Workspace Planner card is handed to an interactive session.
        """
        flat = " ".join(text.split())
        if not flat:
            return False
        ok = self.write(flat)
        if ok and submit:
            # A short gap lets Claude's input box register the text before Enter.
            asyncio.get_running_loop().call_later(0.15, lambda: self.write("\r"))
        return ok

    def interrupt(self) -> bool:
        """Send ctrl+c -- the same "stop what you are doing" as in a real terminal."""
        return self.write("\x03")

    def paste(self, text: str) -> bool:
        """Type clipboard text into the console.

        Newlines become carriage returns, the way a real terminal delivers them.  When
        the application has asked for bracketed paste, the block is wrapped in the paste
        markers so a multi-line paste arrives as one unit -- otherwise every newline in
        it would submit as its own prompt.
        """
        if not text:
            return False
        body = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r")
        if getattr(self._screen, "bracketed_paste", False):
            body = "\x1b[200~" + body + "\x1b[201~"
        logs.log().info("[%s] pasting %d chars", self.project.name, len(text))
        return self.write(body)

    def visible_text(self) -> str:
        """The console's current screen as plain text, for copying.

        Follows the scroll position, so copying while scrolled back yields the history
        you are looking at rather than the live tail.
        """
        if self._screen is None:
            return ""
        lines = [
            "".join(row[x].data or " " for x in range(self._screen.columns)).rstrip()
            for row in self._visible_rows()
        ]
        return "\n".join(lines).rstrip()

    # -- scrollback -------------------------------------------------------------

    @property
    def _history_lines(self) -> int:
        """How many lines have scrolled off the top and are still retained."""
        if self._screen is None:
            return 0
        history = getattr(self._screen, "history", None)
        return 0 if history is None else len(history.top)

    def _sync_scroll(self) -> None:
        """Re-anchor the offset to the content after new output scrolled the screen.

        ``_scroll_offset`` counts back from the live bottom, but that bottom moves every
        time a line scrolls off.  Left alone the view would slide forward through the
        history while the user was reading it -- so each line that leaves the top pushes
        the offset one further back, keeping the same text on screen.  Clamping to the
        retained history means the view does creep once the scrollback deque saturates,
        which is unavoidable: those lines are genuinely gone.
        """
        screen = self._screen
        if screen is None:
            return
        now = getattr(screen, "lines_scrolled_off", 0)
        if self._scroll_offset:
            drift = now - self._scroll_mark
            if drift > 0:
                self._scroll_offset = min(self._scroll_offset + drift, self._history_lines)
        self._scroll_mark = now

    @property
    def scrolled_back(self) -> int:
        """How many lines above the live view are currently being shown (0 = live)."""
        self._sync_scroll()
        return self._scroll_offset

    def _scroll_by(self, lines: int) -> bool:
        """Move the view by ``lines`` (negative = towards older output).

        The offset is clamped to the retained history, so it also self-corrects when
        old lines fall out of the deque while the user sits scrolled back.
        """
        self._sync_scroll()
        target = max(0, min(self._scroll_offset - lines, self._history_lines))
        if target == self._scroll_offset:
            return False
        self._scroll_offset = target
        self._dirty = True
        self._notify()
        return True

    def scroll_up(self, pages: int = 1) -> bool:
        """Scroll towards older output.  False when there is no more history."""
        return self._scroll_by(-self._page_lines(pages))

    def scroll_down(self, pages: int = 1) -> bool:
        """Scroll back towards the live view."""
        return self._scroll_by(self._page_lines(pages))

    def _page_lines(self, pages: int) -> int:
        """Lines per wheel notch / key press -- half a screen, as pyte's ratio was."""
        return max(1, self.rows // 2) * max(1, pages)

    def scroll_to_live(self) -> None:
        """Jump back to the bottom.  New output no longer does this by itself."""
        if self._scroll_offset == 0:
            return
        self._scroll_offset = 0
        self._dirty = True
        self._notify()

    def _visible_rows(self) -> list:
        """The ``rows`` lines currently on show, oldest first.

        With an offset of zero this is just the live buffer.  Scrolled back by N, the
        window starts N lines earlier, so the first rows come from the tail of
        ``history.top`` and the rest from the top of the buffer.  Both are the same
        ``x -> Char`` mappings, so the renderer treats them identically.
        """
        screen = self._screen
        if screen is None:
            return []
        self._sync_scroll()
        offset = min(self._scroll_offset, self._history_lines)
        if offset <= 0:
            return [screen.buffer[y] for y in range(screen.lines)]

        top = screen.history.top
        start = len(top) - offset
        rows = list(islice(top, start, len(top)))
        rows.extend(screen.buffer[y] for y in range(screen.lines - offset))
        return rows[: screen.lines]

    # -- geometry ---------------------------------------------------------------

    def resize(self, cols: int, rows: int) -> None:
        """Resize both the console and the emulator so Claude Code reflows."""
        cols = max(int(cols), 20)
        rows = max(int(rows), 4)
        if cols == self.cols and rows == self.rows:
            return
        self.cols, self.rows = cols, rows
        # The reflow rewrites every retained line, so a held offset no longer points at
        # what the user was reading -- drop back to live rather than show a torn window.
        self._scroll_offset = 0
        if self._screen is not None:
            self._screen.resize(rows, cols)
        if self._pty is not None:
            try:
                self._pty.set_size(cols, rows)
            except Exception:
                pass
        self._dirty = True
        self._notify()

    # -- output -----------------------------------------------------------------

    async def _pump(self) -> None:
        """Read the console without blocking and feed the emulator."""
        assert self._pty is not None
        last_paint = 0.0
        loop = asyncio.get_running_loop()
        try:
            while True:
                data = ""
                try:
                    data = self._pty.read(blocking=False)
                except Exception:
                    if not self.is_running:
                        break
                if data:
                    logs.raw(self.project.name, data)
                    # Drop escape sequences pyte cannot model before they reach it.
                    self._stream.feed(_UNSUPPORTED_CSI_RE.sub("", data))
                    self._dirty = True
                    if self._first_data_at is None:
                        self._first_data_at = loop.time()
                    if not self._launched and (
                        self._ready_for_input()
                        or loop.time() - self._first_data_at > 3.0
                    ):
                        # PowerShell has drawn its prompt: type the startup command.
                        self._launch_claude()
                    # Repaint at a bounded rate rather than once per read.
                    now = loop.time()
                    if now - last_paint >= self.REPAINT_INTERVAL:
                        last_paint = now
                        self._refresh_status()
                        self._notify()
                    continue

                if not self.is_running:
                    break
                if not self._launched and self._first_data_at is not None:
                    # The shell went quiet after printing its prompt -- that quiet IS
                    # the signal that it is waiting for input.
                    if self._ready_for_input() or loop.time() - self._first_data_at > 3.0:
                        self._launch_claude()
                if self._dirty:
                    self._refresh_status()
                    self._notify()
                await asyncio.sleep(self.IDLE_POLL)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            logs.log().exception("[%s] reader crashed", self.project.name)
            self._write_notice(f"console reader stopped: {exc}")
            self._set_status(TerminalStatus.ERROR)
            return

        # The console exited on its own (the user typed `exit`, or claude crashed).
        if self._status is not TerminalStatus.INACTIVE:
            self._set_status(TerminalStatus.INACTIVE)

    def _refresh_status(self) -> None:
        """Derive Idle vs Thinking/Working from what is on screen.

        A real TUI emits no machine-readable events, so we read the same cue a human
        does: Claude Code shows an "esc to interrupt" hint for as long as a turn is
        running.
        """
        if self._screen is None:
            return
        text = "\n".join(self._screen.display).lower()
        busy = any(marker in text for marker in _BUSY_MARKERS)
        self._set_status(TerminalStatus.WORKING if busy else TerminalStatus.IDLE)

    def awaiting_input(self) -> bool:
        """True when Claude appears to be waiting for a permission decision."""
        if self._screen is None:
            return False
        text = "\n".join(self._screen.display).lower()
        return any(marker in text for marker in _PROMPT_MARKERS)

    def _write_notice(self, message: str) -> None:
        """Show an vyctl message on the emulated screen itself."""
        if self._stream is not None:
            self._stream.feed(f"\r\n[vyctl] {message}\r\n")
            self._dirty = True

    # -- rendering --------------------------------------------------------------

    def render(self) -> Text:
        """Snapshot the emulated screen as a Rich renderable."""
        if self._screen is None:
            return Text("not started", style="dim")

        self._dirty = False
        screen = self._screen
        cursor = screen.cursor
        rows = self._visible_rows()
        # While scrolled back the cursor belongs to the live tail, which is off-screen;
        # drawing it would stamp a reverse-video block onto unrelated history.
        show_cursor = self._focused and not cursor.hidden and self._scroll_offset == 0
        result = Text(no_wrap=True, overflow="crop", end="")

        for y, row in enumerate(rows):
            if y:
                result.append("\n")
            # Coalesce runs of identical styling -- one Text.append per run instead of
            # one per character keeps a full-screen repaint cheap.
            run: list[str] = []
            run_style: Style | None = None
            for x in range(screen.columns):
                char = row[x]
                style = _style_for(
                    char.fg,
                    char.bg,
                    char.bold,
                    char.italics,
                    char.reverse,
                    getattr(char, "strikethrough", False),
                    char.blink,
                    char.underscore,
                )
                if show_cursor and y == cursor.y and x == cursor.x:
                    style = style + Style(reverse=True)
                if style != run_style:
                    if run:
                        result.append("".join(run), run_style)
                    run, run_style = [], style
                run.append(char.data or " ")
            if run:
                result.append("".join(run), run_style)
        return result

    def screen_text(self) -> str:
        """Plain-text snapshot (used by tests and status heuristics)."""
        return "\n".join(self._screen.display) if self._screen is not None else ""


def _powershell_path() -> str:
    """Absolute path to powershell.exe (ConPTY wants a real executable path)."""
    from .process import powershell_executable

    return powershell_executable()


# --------------------------------------------------------------------------------------
# Manager
# --------------------------------------------------------------------------------------


class TerminalManager:
    """Owns one :class:`TerminalSession` per project and tears them all down on exit."""

    def __init__(self, config: AppConfig, on_change: TerminalCallback) -> None:
        self.config = config
        self._on_change = on_change
        self._sessions: dict[str, TerminalSession] = {}

    def __iter__(self):
        return iter(self._sessions.values())

    def get(self, project_id: str) -> TerminalSession | None:
        return self._sessions.get(project_id)

    def ensure(self, project: Project, rows: int = 24, cols: int = 80) -> TerminalSession:
        session = self._sessions.get(project.id)
        if session is None:
            session = TerminalSession(project, self.config, self._on_change, rows, cols)
            self._sessions[project.id] = session
        else:
            session.project = project  # pick up edits from the Project Manager tab
        return session

    def start(self, project: Project, rows: int = 24, cols: int = 80) -> bool:
        return self.ensure(project, rows, cols).start()

    async def stop(self, project_id: str) -> None:
        session = self._sessions.get(project_id)
        if session is not None:
            await session.stop()

    async def discard(self, project_id: str) -> None:
        session = self._sessions.pop(project_id, None)
        if session is not None:
            await session.stop()

    async def stop_all(self) -> None:
        sessions = list(self._sessions.values())
        await asyncio.gather(*(s.stop() for s in sessions), return_exceptions=True)

    def running_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.is_running)

    def busy_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.status is TerminalStatus.WORKING)
