"""
Reusable Textual widgets and modal dialogs for vyctl.

Nothing in this module talks to a process directly -- panes render whatever the
:mod:`vyctl.process` layer hands them and post messages upward, so the app class
stays the only place that wires UI events to session control.
"""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import (
    Button,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Select,
    Static,
    Switch,
    TextArea,
)

from . import clipboard
from .config import COLUMN_TITLES, COLUMNS, Project, Task
from .process import SessionStatus
from .terminal import TerminalStatus

#: Sentinel meaning "no option chosen" in a Select.  Textual renamed this from
#: ``Select.BLANK`` to ``Select.NULL`` (and ``Select.BLANK`` is now a plain bool in
#: 8.x), so resolve it once here and keep working on either version.
SELECT_BLANK = getattr(Select, "NULL", getattr(Select, "BLANK", None))

# --------------------------------------------------------------------------------------
# Output styling
# --------------------------------------------------------------------------------------

#: Rich style per output kind emitted by :class:`~vyctl.process.ClaudeSession`.
KIND_STYLES: dict[str, str] = {
    "info": "dim cyan",
    "assistant": "white",
    "thinking": "dim magenta",
    "tool": "yellow",
    "tool_result": "dim green",
    "result": "bold green",
    "stdout": "grey70",
    "stderr": "dim red",
    "error": "bold red",
}

#: CSS class per session status, used to colour the pane badge.
STATUS_CLASSES: dict[SessionStatus, str] = {
    SessionStatus.INACTIVE: "badge-inactive",
    SessionStatus.STARTING: "badge-starting",
    SessionStatus.IDLE: "badge-idle",
    SessionStatus.WORKING: "badge-working",
    SessionStatus.ERROR: "badge-error",
}

#: The same mapping for interactive (ConPTY) sessions.
TERMINAL_STATUS_CLASSES: dict[TerminalStatus, str] = {
    TerminalStatus.INACTIVE: "badge-inactive",
    TerminalStatus.STARTING: "badge-starting",
    TerminalStatus.IDLE: "badge-idle",
    TerminalStatus.WORKING: "badge-working",
    TerminalStatus.ERROR: "badge-error",
}


# --------------------------------------------------------------------------------------
# Tab 1 -- dashboard pane
# --------------------------------------------------------------------------------------


class ProjectPane(Vertical):
    """One dashboard cell: header + status badge, streaming log, and a prompt input."""

    class PromptSubmitted(Message):
        """Posted when the user presses Enter in this pane's input box."""

        def __init__(self, project_id: str, text: str) -> None:
            super().__init__()
            self.project_id = project_id
            self.text = text

    class Focused(Message):
        """Posted when this pane's input gains focus (drives the 'current pane' idea)."""

        def __init__(self, project_id: str) -> None:
            super().__init__()
            self.project_id = project_id

    def __init__(
        self, project: Project, max_log_lines: int = 2000, id: str | None = None  # noqa: A002
    ) -> None:
        super().__init__(id=id or f"pane-{project.id}", classes="project-pane")
        self.project = project
        self._max_log_lines = max_log_lines

    def compose(self) -> ComposeResult:
        with Horizontal(classes="pane-header"):
            yield Static(self.project.name, classes="pane-title")
            yield Static(self._path_hint(), classes="pane-path")
            yield Static("[Inactive]", classes="pane-badge badge-inactive")
        yield RichLog(
            classes="pane-log",
            wrap=True,
            markup=False,
            highlight=False,
            auto_scroll=True,
            min_width=8,
            max_lines=self._max_log_lines,
        )
        yield Input(
            placeholder="prompt + Enter   (prefix ! for a raw PowerShell command)",
            classes="pane-input",
        )

    # -- helpers ----------------------------------------------------------------

    def _path_hint(self) -> str:
        """Shorten the project path so it fits a narrow pane header."""
        path = str(self.project.resolved_path)
        return path if len(path) <= 34 else "..." + path[-31:]

    @property
    def log(self) -> RichLog:
        return self.query_one(RichLog)

    @property
    def input(self) -> Input:
        return self.query_one(Input)

    def write_output(self, kind: str, text: str) -> None:
        """Append a styled line to this pane's log."""
        style = KIND_STYLES.get(kind, "")
        self.log.write(Text(text, style=style) if style else Text(text))

    def write_banner(self, text: str) -> None:
        self.log.write(Text(text, style="bold cyan"))

    def set_status(self, status: SessionStatus, suffix: str = "") -> None:
        """Update the colour-coded badge."""
        badge = self.query_one(".pane-badge", Static)
        badge.remove_class(*STATUS_CLASSES.values())
        badge.add_class(STATUS_CLASSES[status])
        label = f"[{status.label}]"
        badge.update(f"{label} {suffix}".strip())
        self.set_class(status is SessionStatus.WORKING, "pane-busy")

    def clear_log(self) -> None:
        self.log.clear()

    # -- events -----------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        self.post_message(self.PromptSubmitted(self.project.id, text))

    def on_descendant_focus(self) -> None:
        self.post_message(self.Focused(self.project.id))
        self.add_class("pane-active")

    def on_descendant_blur(self) -> None:
        self.remove_class("pane-active")


# --------------------------------------------------------------------------------------
# Tab 1 -- interactive (ConPTY) pane
# --------------------------------------------------------------------------------------


class TerminalView(Widget):
    """Renders a :class:`~vyctl.terminal.TerminalSession` and owns its keyboard.

    While this widget has focus it consumes almost every key and forwards it to the real
    console, which is what makes the embedded Claude Code behave like the one in your own
    terminal.  Two keys are deliberately *not* forwarded so you are never trapped:
    ``f12`` releases focus back to the dashboard, and the app's ``ctrl+q`` binding is
    marked priority so quitting always works.
    """

    can_focus = True

    #: Keys reserved by vyctl -- never handed to the console, so you can always
    #: move focus, switch session, or quit from inside a live Claude session.  Claude
    #: Code itself uses neither function keys nor alt+digit, so nothing is lost.
    RESERVED = (
        {f"f{n}" for n in range(1, 13)}
        | {f"alt+{n}" for n in range(10)}
        # Terminal scrollback, the same keys a real terminal uses.
        | {"shift+pageup", "shift+pagedown", "shift+home", "shift+end"}
    )

    def __init__(self, session, id: str | None = None) -> None:  # noqa: A002
        super().__init__(id=id, classes="terminal-view")
        self.session = session

    def render(self) -> Text:
        return self.session.render()

    # -- geometry ---------------------------------------------------------------

    # -- scrollback -------------------------------------------------------------

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        """Wheel up scrolls into the console's history."""
        if self.session.scroll_up():
            event.stop()
            event.prevent_default()

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if self.session.scroll_down():
            event.stop()
            event.prevent_default()

    # -- clipboard --------------------------------------------------------------

    def on_paste(self, event: events.Paste) -> None:
        """A paste performed by the host terminal (ctrl+shift+v, right-click, menu).

        The host delivers the text to us as one event, so it never has to travel through
        the key encoder.
        """
        if event.text:
            self.session.paste(event.text)
            event.stop()
            event.prevent_default()

    def _handle_clipboard_key(self, key: str) -> bool:
        """ctrl+v pastes from the Windows clipboard; True if the key was consumed.

        In a normal terminal ctrl+v means "quote the next key", which is of no use in
        Claude Code, so it is far more valuable as paste -- that is what people press.
        Copying is f8, because ctrl+c has to stay as interrupt.
        """
        if key not in ("ctrl+v", "ctrl+shift+v"):
            return False
        text = clipboard.read_text()
        if text:
            self.session.paste(text)
        else:
            self.notify("Clipboard is empty (or holds no text).", severity="warning")
        return True

    def _handle_scroll_key(self, key: str) -> bool:
        """shift+pageup/pagedown/home/end move through history; True if consumed."""
        if key == "shift+pageup":
            self.session.scroll_up()
        elif key == "shift+pagedown":
            self.session.scroll_down()
        elif key == "shift+home":
            self.session.scroll_up(pages=999)
        elif key == "shift+end":
            self.session.scroll_to_live()
        else:
            return False
        return True

    def on_resize(self) -> None:
        """Keep the pseudo-console the same size as the widget, so Claude reflows."""
        width, height = self.content_size
        if width > 0 and height > 0:
            self.session.resize(width, height)

    # -- keyboard ---------------------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        if self._handle_clipboard_key(event.key):
            event.stop()
            event.prevent_default()
            return
        if self._handle_scroll_key(event.key):
            event.stop()
            event.prevent_default()
            return
        if event.key in self.RESERVED:
            return  # let the pane/app handle it
        if self.session.send_key(event.key, event.character):
            # Stop Textual from also acting on the key (tab must indent in Claude's
            # prompt, not move focus out of the pane).
            event.stop()
            event.prevent_default()

    def on_focus(self) -> None:
        self.session.set_focused(True)

    def on_blur(self) -> None:
        self.session.set_focused(False)


# --------------------------------------------------------------------------------------
# Sidebar lists
# --------------------------------------------------------------------------------------

#: One-glyph column marker, so a task's state reads at a glance in a narrow sidebar.
COLUMN_GLYPHS = {"backlog": "○", "in_progress": "◐", "completed": "●"}

#: Column marker colours.
COLUMN_STYLES = {"backlog": "grey62", "in_progress": "yellow", "completed": "green"}


class ProjectItem(ListItem):
    """A project row in the sidebar: status dot, name, and badge."""

    def __init__(self, project: Project, status_label: str, status_class: str) -> None:
        super().__init__()
        self.project = project
        self.status_label = status_label
        self.status_class = status_class

    def compose(self) -> ComposeResult:
        yield Static(self._body(), classes="sidebar-row")

    def _body(self) -> Text:
        colour = {
            "badge-idle": "green",
            "badge-working": "yellow",
            "badge-starting": "cyan",
            "badge-error": "red",
        }.get(self.status_class, "grey50")
        body = Text()
        body.append("● " if self.project.active else "○ ", style=colour)
        body.append(self.project.name, style="bold" if self.project.active else "dim")
        body.append(f"\n   {self.status_label}", style=f"dim {colour}")
        return body

    def update_status(self, status_label: str, status_class: str) -> None:
        """Refresh just this row -- cheaper than rebuilding the whole list."""
        self.status_label = status_label
        self.status_class = status_class
        try:
            self.query_one(".sidebar-row", Static).update(self._body())
        except Exception:
            pass


class TaskItem(ListItem):
    """A TODO row in the sidebar.

    The task is stored as ``card`` rather than ``task`` -- Textual's ``MessagePump``
    already owns a ``task`` property (the widget's asyncio task).
    """

    def __init__(self, card: Task, project_name: str) -> None:
        super().__init__()
        self.card = card
        self.project_name = project_name

    def compose(self) -> ComposeResult:
        body = Text()
        column = self.card.column
        body.append(
            COLUMN_GLYPHS.get(column, "○") + " ",
            style=COLUMN_STYLES.get(column, "grey62"),
        )
        title = self.card.title
        body.append(
            title if len(title) <= 30 else title[:27] + "...",
            style="strike dim" if column == "completed" else "",
        )
        body.append(f"\n   {self.project_name}", style="dim cyan")
        yield Static(body)


# --------------------------------------------------------------------------------------
# Modal dialogs
# --------------------------------------------------------------------------------------


class ProjectForm(ModalScreen[dict | None]):
    """Add/edit dialog for a project (Tab 2)."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, project: Project | None = None) -> None:
        super().__init__()
        self.project = project

    def compose(self) -> ComposeResult:
        editing = self.project is not None
        with Vertical(classes="dialog"):
            yield Label("Edit project" if editing else "New project", classes="dialog-title")

            yield Label("Project name")
            yield Input(
                value=self.project.name if self.project else "",
                placeholder="e.g. swarmdrones",
                id="field-name",
            )

            yield Label("Local absolute folder path")
            yield Input(
                value=self.project.path if self.project else "",
                placeholder=r"D:\workspace\personal\swarmdrones",
                id="field-path",
            )
            yield Static("", id="path-hint", classes="dialog-hint")

            yield Label("Startup command (the Claude CLI invocation)")
            yield Input(
                value=self.project.command if self.project else "claude",
                placeholder="claude",
                id="field-command",
            )

            yield Label("Extra CLI arguments (optional)")
            yield Input(
                value=self.project.extra_args if self.project else "",
                placeholder="--permission-mode acceptEdits",
                id="field-extra",
            )

            with Horizontal(classes="dialog-row"):
                yield Label("Active (spawn a session for this project)")
                yield Switch(value=self.project.active if self.project else True, id="field-active")

            with Horizontal(classes="dialog-buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#field-name", Input).focus()
        self._validate_path()

    def _validate_path(self) -> None:
        """Warn (but never block) when the folder does not exist yet."""
        raw = self.query_one("#field-path", Input).value.strip()
        hint = self.query_one("#path-hint", Static)
        if not raw:
            hint.update("")
            return
        try:
            exists = Path(raw).expanduser().is_dir()
        except OSError:
            exists = False
        hint.update(
            Text("folder found", style="green")
            if exists
            else Text("warning: folder not found -- the session will fail to start", style="yellow")
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "field-path":
            self._validate_path()

    def on_input_submitted(self) -> None:
        self._save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self._save()
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _save(self) -> None:
        name = self.query_one("#field-name", Input).value.strip()
        path = self.query_one("#field-path", Input).value.strip()
        if not name or not path:
            self.query_one("#path-hint", Static).update(
                Text("name and folder path are both required", style="bold red")
            )
            return
        self.dismiss(
            {
                "name": name,
                "path": path,
                "command": self.query_one("#field-command", Input).value.strip() or "claude",
                "extra_args": self.query_one("#field-extra", Input).value.strip(),
                "active": self.query_one("#field-active", Switch).value,
            }
        )


class TaskForm(ModalScreen[dict | None]):
    """Add/edit dialog for a kanban card (Tab 3)."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(
        self,
        projects: list[Project],
        card: Task | None = None,
        default_column: str = "backlog",
    ) -> None:
        super().__init__()
        self.projects = projects
        # Named ``card``, not ``task``: ``MessagePump.task`` is reserved by Textual.
        self.card = card
        self.default_column = default_column

    def compose(self) -> ComposeResult:
        editing = self.card is not None
        project_options = [(p.name, p.id) for p in self.projects]
        with Vertical(classes="dialog"):
            yield Label("Edit task" if editing else "New task", classes="dialog-title")

            yield Label("Title")
            yield Input(
                value=self.card.title if self.card else "",
                placeholder="What should happen?",
                id="field-title",
            )

            yield Label("Linked project")
            yield Select(
                project_options,
                prompt="(unassigned)",
                allow_blank=True,
                value=(
                    self.card.project_id
                    if self.card and self.card.project_id
                    else SELECT_BLANK
                ),
                id="field-project",
            )

            yield Label("Column")
            yield Select(
                [(COLUMN_TITLES[c], c) for c in COLUMNS],
                allow_blank=False,
                value=self.card.column if self.card else self.default_column,
                id="field-column",
            )

            yield Label("Notes (optional)")
            yield TextArea(self.card.notes if self.card else "", id="field-notes")

            with Horizontal(classes="dialog-buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#field-title", Input).focus()

    def on_input_submitted(self) -> None:
        self._save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self._save()
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _save(self) -> None:
        title = self.query_one("#field-title", Input).value.strip()
        if not title:
            return
        project_value = self.query_one("#field-project", Select).value
        column_value = self.query_one("#field-column", Select).value
        self.dismiss(
            {
                "title": title,
                "project_id": None if project_value is SELECT_BLANK else str(project_value),
                "column": str(column_value) if column_value is not SELECT_BLANK else "backlog",
                "notes": self.query_one("#field-notes", TextArea).text.strip(),
            }
        )


class ConfirmDialog(ModalScreen[bool]):
    """Yes/no confirmation used before destructive actions."""

    BINDINGS = [("escape", "cancel", "Cancel"), ("y", "confirm", "Yes"), ("n", "cancel", "No")]

    def __init__(self, question: str, detail: str = "", confirm_label: str = "Delete") -> None:
        super().__init__()
        self.question = question
        self.detail = detail
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog dialog-small"):
            yield Label(self.question, classes="dialog-title")
            if self.detail:
                yield Static(self.detail, classes="dialog-hint")
            with Horizontal(classes="dialog-buttons"):
                yield Button(self.confirm_label, variant="error", id="confirm")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class SearchDialog(ModalScreen[tuple | None]):
    """Search every session's scrollback at once (``f6``).

    Dismisses with ``(project_id, line_index)`` for the chosen hit, or None.  The
    search itself is done by the caller -- this screen only renders the results, so it
    stays ignorant of how sessions are stored.
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, search) -> None:
        super().__init__()
        #: ``query -> [(project_id, project_name, line_index, text), ...]``
        self._search = search
        self._hits: list[tuple] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Search all sessions", classes="dialog-title")
            yield Input(placeholder="text to find", id="search-query")
            yield Static("", id="search-count", classes="dialog-hint")
            with VerticalScroll():
                yield ListView(id="search-results")

    def on_mount(self) -> None:
        self.query_one("#search-query", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._render_hits(self._search(event.value))

    def _render_hits(self, hits: list[tuple]) -> None:
        self._hits = hits
        view = self.query_one("#search-results", ListView)
        view.clear()
        for _pid, name, _index, text in hits[:200]:
            view.append(ListItem(Label(f"{name}  {text.strip()[:70]}")))
        count = self.query_one("#search-count", Static)
        if not hits:
            count.update("no matches")
        else:
            shown = min(len(hits), 200)
            more = "" if shown == len(hits) else f" (showing first {shown})"
            count.update(f"{len(hits)} matches{more} -- enter to jump")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter from the query box jumps straight to the first hit."""
        if self._hits:
            pid, _name, index, _text = self._hits[0]
            self.dismiss((pid, index))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        view = self.query_one("#search-results", ListView)
        position = view.index or 0
        if 0 <= position < len(self._hits):
            pid, _name, index, _text = self._hits[position]
            self.dismiss((pid, index))

    def action_cancel(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    """Keyboard reference (``?``)."""

    BINDINGS = [("escape", "close", "Close"), ("question_mark", "close", "Close")]

    HELP = """\
The left sidebar holds everything textual; the right side is just the console.
Every session keeps running in the background whether or not it is on screen.

TO ADD A SESSION:  press f2 (focus the SESSIONS list), then 'a'.
Fill in a name and the project folder, then Save -- the console starts itself.

Single-letter keys act on whichever sidebar list has focus, so press f2 or f3
first.  The bottom of the sidebar always shows the keys that are live for it.

ALWAYS AVAILABLE  (these work even while a console has the keyboard)
  f1            focus the console
  f2            focus the SESSIONS list      f3   focus the TODO list
  f4 / ?        this help
  f5            restart the selected session (also aborts a running turn)
  f6            search every session's scrollback and jump to a hit
  f9            hide/show the sidebar -- console takes the whole window
  f10           switch console mode: real terminal <-> headless log
  f12           release the keyboard back to the sidebar
  alt+1 .. 9    jump straight to the Nth session
  ctrl+q        quit (kills every background powershell + claude)

IN THE CONSOLE
  Everything else goes to the real Claude Code session: type, enter, arrows,
  shift+tab to cycle modes, ctrl+c to interrupt, /slash commands -- all of it
  behaves exactly as it does in your own terminal.

  mouse wheel        scroll back through the console history
  shift+pageup/down  the same, by the keyboard
  shift+home         jump to the oldest line kept
  shift+end          jump back to live output
  Output no longer drags the view down: history stays put while a session
  keeps writing.  Typing returns you to live.

CLIPBOARD
  ctrl+v      paste the Windows clipboard into the prompt.  Multi-line text
              arrives as one block, it does not submit a turn per line.
  f8          copy what the console is showing to the clipboard.
  ctrl+c      still means INTERRUPT, as in any terminal -- that is why copy
              is f8 and not ctrl+c.
  You can also select with the mouse the way your terminal normally does
  (in Windows Terminal, hold shift while dragging) and copy with its own
  shortcut; vyctl forwards any paste the terminal performs for you.

SESSIONS LIST  (f2)
  up/down  select -- the console on the right follows the selection
  enter    hand the keyboard to that session's console
  a  add     e  edit     d  delete     space  active on/off
  s  start   x  stop     r  restart

TODO LIST  (f3)
  n  new     e  edit     d  delete
  h / l      move between Backlog -> In Progress -> Completed
  p          send the task to its linked project's Claude session
             (types it into that console and marks it In Progress)
  c          mark Completed

DEBUG LOG
  Off by default.  Start with --debug (or set "log_level": "debug" in
  config.json) to write vyctl.log next to the config: session pids,
  the exact launch command, status changes, teardown, crashes.
  --debug-raw additionally records every byte the consoles emit, which is
  what to use for a rendering problem -- it contains your session text,
  so turn it off afterwards.

HEADLESS MODE ONLY  (f10)
  The console is replaced by a clean log plus a prompt box.
  Prefix a line with ! to run it as a raw PowerShell command.
"""

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog dialog-help"):
            yield Label("vyctl -- keyboard reference", classes="dialog-title")
            with VerticalScroll():
                yield Static(self.HELP)
            yield Button("Close", variant="primary", id="close")

    def on_button_pressed(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
