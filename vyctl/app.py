"""
vyctl -- the Textual application itself.

One screen, two columns:

* **Left sidebar (~30%)** -- everything textual: the project/session list with live status,
  the TODO list, and the technical details of whatever is selected.
* **Right stage (~70%)** -- nothing but the console.  The selected project's Claude
  session fills it edge to edge, so it reads like your own terminal rather than a widget
  in a dashboard.

Every project keeps running in the background whether or not its console is on screen;
selecting a different project simply swaps which one the stage shows.

Run with ``python -m vyctl``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import ContentSwitcher, Footer, Label, ListView, Static

from . import __version__, clipboard, logs, terminal
from .config import COLUMN_TITLES, COLUMNS, ConfigStore, Project, Task
from .process import ClaudeSession, SessionManager, SessionStatus
from .terminal import TerminalManager, TerminalSession, TerminalStatus
from .widgets import (
    STATUS_CLASSES,
    TERMINAL_STATUS_CLASSES,
    ConfirmDialog,
    HelpScreen,
    ProjectForm,
    ProjectItem,
    ProjectPane,
    TaskForm,
    TaskItem,
    TerminalView,
)


class VyctlApp(App[None]):
    """Multi-project dashboard for parallel Claude Code sessions."""

    TITLE = "vyctl"
    CSS_PATH = "app.tcss"

    BINDINGS = [
        # Always available, even while a console owns the keyboard.
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("f1", "focus_console", "Console"),
        Binding("f2", "focus_projects", "Projects"),
        Binding("f3", "focus_tasks", "Tasks"),
        Binding("f4", "help", "Help"),
        Binding("f5", "restart_session", "Restart", show=False),
        Binding("f8", "copy_console", "Copy", show=False),
        Binding("f9", "toggle_sidebar", "Sidebar", show=False),
        Binding("f10", "toggle_pane_mode", "Mode", show=False),
        Binding("f12", "focus_projects", "Release keyboard", show=False),
        Binding("question_mark", "help", "Help", show=False),
        # Jump straight to a project without leaving the console.
        *[
            Binding(f"alt+{n}", f"select_index({n - 1})", f"Project {n}", show=False)
            for n in range(1, 10)
        ],
        # Sidebar verbs -- only meaningful when a sidebar list has focus.
        Binding("a", "add", "Add", show=False),
        Binding("n", "add", "New", show=False),
        Binding("e", "edit", "Edit", show=False),
        Binding("d", "delete", "Delete", show=False),
        Binding("space", "toggle_active", "Toggle active", show=False),
        Binding("s", "start_session", "Start", show=False),
        Binding("x", "stop_session", "Stop", show=False),
        Binding("r", "restart_session", "Restart", show=False),
        Binding("h", "move_task(-1)", "Move left", show=False),
        Binding("l", "move_task(1)", "Move right", show=False),
        Binding("p", "send_task", "Send to Claude", show=False),
        Binding("c", "complete_task", "Complete", show=False),
    ]

    def __init__(self, config_path: str | None = None) -> None:
        super().__init__()
        self.store = ConfigStore(config_path)
        self.config = self.store.load()
        #: Headless (pipe) sessions -- used by "headless" pane mode.
        self.sessions = SessionManager(
            self.config,
            on_output=self._on_session_output,
            on_status=self._on_session_status,
        )
        #: Interactive (ConPTY) sessions -- used by "interactive" pane mode.
        self.terminals = TerminalManager(self.config, on_change=self._on_terminal_change)
        #: Debug log (off unless log_level is set in config.json).
        self.log_path = logs.setup(
            self.store.path.parent,
            level=self.config.log_level,
            raw_stream=self.config.log_raw_stream,
        )
        logs.log().info(
            "vyctl %s starting: mode=%s projects=%d config=%s",
            __version__, self.config.pane_mode, len(self.config.projects), self.store.path,
        )
        #: project id -> the stage widget showing that project.
        self._stages: dict[str, ProjectPane | TerminalView] = {}
        #: The project currently on the stage.
        self._selected: str | None = None
        self._tearing_down = False

    # ----------------------------------------------------------------- layout --

    def compose(self) -> ComposeResult:
        with Horizontal(id="root"):
            with Vertical(id="sidebar"):
                yield Static("", id="brand")
                yield Label("SESSIONS", classes="side-heading")
                yield ListView(id="project-list", classes="side-list")
                yield Label("TODO", classes="side-heading")
                yield ListView(id="task-list", classes="side-list")
                yield Label("DETAILS", classes="side-heading")
                with VerticalScroll(id="details-wrap"):
                    yield Static("", id="details")
                # Which single-letter keys are live depends on what has focus, so spell
                # it out here rather than only inside the help modal.
                yield Static("", id="hints")
            with Vertical(id="stage"):
                yield Static("", id="stage-title")
                yield ContentSwitcher(id="stage-body")
        yield Footer()

    async def on_mount(self) -> None:
        self._update_brand()
        await self._rebuild_stage()
        self._refresh_projects()
        self._refresh_tasks()
        self._refresh_details()
        self._refresh_hints()

        if self.store.load_error:
            self.notify(
                f"Could not read config: {self.store.load_error}",
                title="Starting with defaults",
                severity="warning",
                timeout=12,
            )
        if not terminal.AVAILABLE and self.config.pane_mode == "interactive":
            self.notify(
                "Interactive consoles need pywinpty + pyte; using headless panes.\n"
                "  .\\.venv\\Scripts\\python.exe -m pip install pywinpty pyte",
                severity="warning",
                timeout=12,
            )
        if not self.config.projects:
            self.notify(
                "No projects yet -- press f2 then 'a' to add your first folder.",
                title="Welcome to vyctl",
                timeout=12,
            )
        elif self.config.autostart:
            self.run_worker(self._autostart(), exclusive=False)
        self.set_focus(self.query_one("#project-list", ListView))

    async def _autostart(self) -> None:
        """Bring every active project's session up in the background."""
        projects = self.config.active_projects()
        if self.interactive:
            # Each console launches `claude --continue` itself, so auto-resume is
            # inherent here (see TerminalSession._startup_command).
            for project in projects:
                self.terminals.start(project)
        else:
            await self.sessions.start_all(projects)
            if self.config.resume_probe:
                for project in projects:
                    session = self.sessions.get(project.id)
                    if session is not None and session.is_running:
                        await session.resume_probe()
        self._refresh_projects()
        self._refresh_details()

    #: Keys that are live for each focus target.  Single-letter verbs act on whichever
    #: sidebar list has focus, which is the one thing the help modal cannot make obvious.
    HINTS = {
        "projects": (
            "SESSIONS  a add · e edit · d delete · space on/off\n"
            "          s start · x stop · r restart · enter opens console"
        ),
        "tasks": (
            "TODO  n new · e edit · d delete · c done\n"
            "      h/l move column · p send to Claude"
        ),
        "console": (
            "CONSOLE has the keyboard — keys go to Claude\n"
            "f2 sessions · f3 todo · f12 release · f9 wide · f4 help"
        ),
    }

    def _refresh_hints(self) -> None:
        """Show the keys that are actually live right now."""
        try:
            hints = self.query_one("#hints", Static)
        except Exception:
            return
        hints.update(self.HINTS.get(self._focus_target(), self.HINTS["console"]))

    def on_descendant_focus(self) -> None:
        self._refresh_hints()

    def _update_brand(self) -> None:
        mode = "interactive" if self.interactive else "headless"
        self.query_one("#brand", Static).update(
            f"vyctl v{__version__}\n{len(self.config.projects)} projects · {mode}"
        )

    # ------------------------------------------------------------ mode helpers --

    @property
    def interactive(self) -> bool:
        """True when the stage embeds a real console instead of a headless log."""
        return self.config.pane_mode == "interactive" and terminal.AVAILABLE

    def _status_of(self, project_id: str):
        """Status object for a project in whichever mode is active."""
        if self.interactive:
            session = self.terminals.get(project_id)
            return session.status if session else TerminalStatus.INACTIVE
        session = self.sessions.get(project_id)
        return session.status if session else SessionStatus.INACTIVE

    def _status_class(self, project_id: str) -> str:
        status = self._status_of(project_id)
        table = TERMINAL_STATUS_CLASSES if self.interactive else STATUS_CLASSES
        return table.get(status, "badge-inactive")

    def _pid_of(self, project_id: str) -> int | None:
        manager = self.terminals if self.interactive else self.sessions
        session = manager.get(project_id)
        return session.pid if session else None

    async def _start_project(self, project: Project) -> bool:
        if self.interactive:
            return self.terminals.start(project)
        return await self.sessions.start(project)

    async def _stop_project(self, project_id: str) -> None:
        await self.terminals.stop(project_id)
        await self.sessions.stop(project_id)

    async def _discard_project(self, project_id: str) -> None:
        await self.terminals.discard(project_id)
        await self.sessions.discard(project_id)

    # ----------------------------------------------------------------- stage --

    async def _rebuild_stage(self) -> None:
        """Create one stage widget per active project; show the selected one."""
        body = self.query_one("#stage-body", ContentSwitcher)
        body.current = None
        await body.remove_children()
        self._stages.clear()

        projects = self.config.active_projects()
        if not projects:
            await body.mount(
                Static(
                    "No active sessions.\n\n"
                    "Press f2 to focus the sidebar, then 'a' to add a project folder.",
                    id="stage-empty",
                    classes="empty-hint",
                )
            )
            body.current = "stage-empty"
            self._selected = None
            self._refresh_stage_title()
            return

        for project in projects:
            widget_id = f"stage-{project.id}"
            if self.interactive:
                session = self.terminals.ensure(project)
                widget: ProjectPane | TerminalView = TerminalView(session, id=widget_id)
            else:
                widget = ProjectPane(
                    project, max_log_lines=self.config.max_log_lines, id=widget_id
                )
            self._stages[project.id] = widget
            await body.mount(widget)

        # Keep the previous selection if it is still around.
        if self._selected not in self._stages:
            self._selected = projects[0].id
        # ContentSwitcher only hides the children it had at mount time, and these were
        # mounted afterwards -- so set visibility explicitly rather than relying on it.
        self._apply_stage_visibility()
        body.current = f"stage-{self._selected}"
        self._refresh_stage_title()

    def _apply_stage_visibility(self) -> None:
        """Exactly one console occupies the stage; the rest keep running unseen."""
        for project_id, widget in self._stages.items():
            widget.display = project_id == self._selected

    def _select_project(self, project_id: str) -> None:
        """Swap which project's console occupies the stage."""
        if project_id not in self._stages or project_id == self._selected:
            self._selected = project_id if project_id in self._stages else self._selected
            self._refresh_stage_title()
            self._refresh_details()
            return
        self._selected = project_id
        self._apply_stage_visibility()
        self.query_one("#stage-body", ContentSwitcher).current = f"stage-{project_id}"
        self._refresh_stage_title()
        self._refresh_details()

    def _refresh_stage_title(self) -> None:
        title = self.query_one("#stage-title", Static)
        project = self.config.project(self._selected)
        if project is None:
            title.update("no session")
            return
        status = self._status_of(project.id)
        pid = self._pid_of(project.id)
        bits = [f"[{status.label}]"]
        if pid:
            bits.append(f"pid {pid}")
        # Make it obvious when the pane is showing history rather than live output.
        session = self.terminals.get(project.id) if self.interactive else None
        if session is not None and session.scrolled_back:
            bits.append(f"SCROLLED BACK {session.scrolled_back} lines (shift+end = live)")
        if self.interactive:
            bits.append("f1 console · f2 sessions · f12 release")
        title.update(f"{project.name}   {'  ·  '.join(bits)}")

    @property
    def _stage_widget(self) -> ProjectPane | TerminalView | None:
        return self._stages.get(self._selected or "")

    # ------------------------------------------------------ session callbacks --

    def _on_session_output(self, session: ClaudeSession, kind: str, text: str) -> None:
        """Route one line of headless output into that project's stage widget."""
        widget = self._stages.get(session.project.id)
        if isinstance(widget, ProjectPane) and widget.is_mounted:
            widget.write_output(kind, text)

    def _on_session_status(self, session: ClaudeSession, status: SessionStatus) -> None:
        widget = self._stages.get(session.project.id)
        if isinstance(widget, ProjectPane) and widget.is_mounted:
            suffix = f"${session.last_cost_usd:.3f}" if session.last_cost_usd else ""
            widget.set_status(status, suffix)
        self._refresh_project_row(session.project.id)
        if session.project.id == self._selected:
            self._refresh_stage_title()
            self._refresh_details()

    def _on_terminal_change(self, session: TerminalSession) -> None:
        """An interactive session repainted or changed state."""
        widget = self._stages.get(session.project.id)
        if isinstance(widget, TerminalView) and widget.is_mounted:
            widget.refresh()
        self._refresh_project_row(session.project.id)
        if session.project.id == self._selected:
            self._refresh_stage_title()

    # ---------------------------------------------------------------- sidebar --

    def _refresh_projects(self) -> None:
        """Rebuild the session list, keeping the cursor where it was."""
        view = self.query_one("#project-list", ListView)
        previous = view.index or 0
        view.clear()
        for project in self.config.projects:
            view.append(
                ProjectItem(
                    project,
                    self._status_of(project.id).label,
                    self._status_class(project.id),
                )
            )
        if self.config.projects:
            target = min(previous, len(self.config.projects) - 1)
            self.call_after_refresh(self._restore_index, view, target)
        self._update_brand()

    def _refresh_project_row(self, project_id: str) -> None:
        """Update one row's badge in place -- no list rebuild, no cursor jump."""
        try:
            view = self.query_one("#project-list", ListView)
        except Exception:
            return
        for item in view.children:
            if isinstance(item, ProjectItem) and item.project.id == project_id:
                item.update_status(
                    self._status_of(project_id).label, self._status_class(project_id)
                )
                return

    def _refresh_tasks(self) -> None:
        view = self.query_one("#task-list", ListView)
        previous = view.index or 0
        view.clear()
        # Group by column so the sidebar reads Backlog -> In progress -> Done.
        ordered = [task for column in COLUMNS for task in self.config.tasks_in(column)]
        for task in ordered:
            view.append(TaskItem(task, self.config.project_name(task.project_id)))
        if ordered:
            self.call_after_refresh(self._restore_index, view, min(previous, len(ordered) - 1))

    def _restore_index(self, view: ListView, index: int) -> None:
        try:
            view.index = index
        except Exception:  # list changed again before the refresh landed
            pass

    def _refresh_details(self) -> None:
        """The technical block: paths, pids, costs, counts."""
        try:
            details = self.query_one("#details", Static)
        except Exception:
            return
        project = self.config.project(self._selected)
        lines: list[str] = []
        if project is None:
            lines.append("no session selected")
        else:
            status = self._status_of(project.id)
            pid = self._pid_of(project.id)
            lines += [
                f"folder   {project.resolved_path}",
                f"command  {project.command}",
            ]
            if project.extra_args:
                lines.append(f"args     {project.extra_args}")
            lines += [
                f"status   {status.label}",
                f"pid      {pid or '-'}",
            ]
            if not project.path_exists():
                lines.append("WARNING  folder not found")
            if not self.interactive:
                session = self.sessions.get(project.id)
                if session is not None:
                    lines += [
                        f"turns    {session.turns}",
                        f"last     ${session.last_cost_usd:.4f}",
                    ]
                    if session.session_id:
                        lines.append(f"session  {session.session_id[:8]}")

        manager = self.terminals if self.interactive else self.sessions
        counts = {column: len(self.config.tasks_in(column)) for column in COLUMNS}
        lines += [
            "",
            f"running  {manager.running_count()}/{len(self.config.active_projects())}",
            f"working  {manager.busy_count()}",
            "todo     "
            + "  ".join(f"{COLUMN_TITLES[c].split(' ')[0]} {counts[c]}" for c in COLUMNS),
            f"config   {self.store.path.name}",
        ]
        if self.log_path is not None:
            lines.append(f"log      {self.log_path.name} ({self.config.log_level})")
        details.update("\n".join(lines))

    # -- selection events -------------------------------------------------------

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Moving the cursor in the session list swaps the console on the stage."""
        if event.list_view.id == "project-list" and isinstance(event.item, ProjectItem):
            project = event.item.project
            if project.active:
                self._select_project(project.id)
            else:
                self._refresh_details()
        elif event.list_view.id == "task-list":
            self._refresh_details()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Enter on a session hands the keyboard straight to its console."""
        if event.list_view.id == "project-list" and isinstance(event.item, ProjectItem):
            if event.item.project.active:
                self._select_project(event.item.project.id)
                self.action_focus_console()

    # -- headless prompt box ----------------------------------------------------

    async def on_project_pane_prompt_submitted(self, event: ProjectPane.PromptSubmitted) -> None:
        """Enter in a headless pane's input: send to Claude, or to the shell with ``!``."""
        project = self.config.project(event.project_id)
        if project is None:
            return
        if not event.text.startswith("!") and self._conflicting_console(project):
            return
        session = self.sessions.ensure(project)
        if event.text.startswith("!"):
            await session.send_shell_line(event.text[1:])
        else:
            await session.send_prompt(event.text)

    # --------------------------------------------------------------- focus --

    def action_focus_console(self) -> None:
        widget = self._stage_widget
        if widget is None:
            return
        if isinstance(widget, ProjectPane):
            widget.input.focus()
        else:
            widget.focus()

    def action_focus_projects(self) -> None:
        self.set_focus(self.query_one("#project-list", ListView))
        self._refresh_hints()

    def action_focus_tasks(self) -> None:
        self.set_focus(self.query_one("#task-list", ListView))
        self._refresh_hints()

    def action_copy_console(self) -> None:
        """f8: copy what the console is showing to the clipboard.

        ctrl+c cannot be used for this -- inside a terminal it has to stay "interrupt"
        -- so copying gets its own key.
        """
        widget = self._stage_widget
        text = ""
        if isinstance(widget, TerminalView):
            text = widget.session.visible_text()
        elif isinstance(widget, ProjectPane):
            text = "\n".join(str(line) for line in widget.log.lines)
        if not text.strip():
            self.notify("Nothing to copy.", severity="warning")
            return
        if clipboard.write_text(text):
            self.notify(f"Copied {len(text.splitlines())} lines to the clipboard.")
        else:
            # OSC 52 via the host terminal, in case Set-Clipboard was unavailable.
            self.copy_to_clipboard(text)
            self.notify("Copied via the terminal's clipboard.")

    def action_toggle_sidebar(self) -> None:
        """Hide the sidebar to give the console the whole window."""
        sidebar = self.query_one("#sidebar", Vertical)
        sidebar.toggle_class("collapsed")
        if sidebar.has_class("collapsed"):
            self.action_focus_console()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_select_index(self, index: int) -> None:
        """alt+N: jump to the Nth active project without leaving the console."""
        projects = self.config.active_projects()
        if 0 <= index < len(projects):
            self._select_project(projects[index].id)
            self.action_focus_console()

    # -- which sidebar list has focus -------------------------------------------

    def _focus_target(self) -> str:
        """``"projects"``, ``"tasks"``, or ``"console"`` -- who owns the keyboard."""
        node = self.focused
        while node is not None:
            node_id = getattr(node, "id", None)
            if node_id == "project-list":
                return "projects"
            if node_id == "task-list":
                return "tasks"
            if node_id == "stage":
                return "console"
            node = node.parent
        return "console"

    def _selected_project(self) -> Project | None:
        view = self.query_one("#project-list", ListView)
        if view.index is None:
            return self.config.project(self._selected)
        try:
            item = view.children[view.index]
        except IndexError:
            return self.config.project(self._selected)
        return item.project if isinstance(item, ProjectItem) else None

    def _selected_task(self) -> Task | None:
        view = self.query_one("#task-list", ListView)
        if view.index is None:
            return None
        try:
            item = view.children[view.index]
        except IndexError:
            return None
        return item.card if isinstance(item, TaskItem) else None

    # ------------------------------------------------------- project actions --

    @work
    async def _add_project(self) -> None:
        result = await self.push_screen_wait(ProjectForm())
        if not result:
            return
        project = self.store.add_project(Project(**result))
        await self._rebuild_stage()
        self._refresh_projects()
        self._refresh_tasks()
        if project.active:
            await self._start_project(project)
        self._refresh_details()
        self.notify(f"Added '{project.name}'.")

    @work
    async def _edit_project(self) -> None:
        project = self._selected_project()
        if project is None:
            return
        result = await self.push_screen_wait(ProjectForm(project))
        if not result:
            return
        was_active = project.active
        path_changed = result["path"] != project.path
        updated = self.store.update_project(project.id, **result)
        if updated is None:
            return
        # The working directory is fixed when the session spawns, so a path change (or
        # being deactivated) means the old session has to go.
        if path_changed or (was_active and not updated.active):
            await self._discard_project(project.id)
        await self._rebuild_stage()
        self._refresh_projects()
        self._refresh_tasks()
        if updated.active and not self._pid_of(updated.id):
            await self._start_project(updated)
        self._refresh_details()
        self.notify(f"Updated '{updated.name}'.")

    @work
    async def _delete_project(self) -> None:
        project = self._selected_project()
        if project is None:
            return
        confirmed = await self.push_screen_wait(
            ConfirmDialog(
                f"Delete project '{project.name}'?",
                "Its session is stopped and linked tasks become unassigned. "
                "Nothing on disk is touched.",
            )
        )
        if not confirmed:
            return
        await self._discard_project(project.id)
        self.store.delete_project(project.id)
        await self._rebuild_stage()
        self._refresh_projects()
        self._refresh_tasks()
        self._refresh_details()
        self.notify(f"Deleted '{project.name}'.")

    async def action_toggle_active(self) -> None:
        if self._focus_target() != "projects":
            return
        project = self._selected_project()
        if project is None:
            return
        updated = self.store.update_project(project.id, active=not project.active)
        if updated is None:
            return
        if not updated.active:
            await self._discard_project(updated.id)
        await self._rebuild_stage()
        if updated.active:
            await self._start_project(updated)
        self._refresh_projects()
        self._refresh_details()

    async def action_start_session(self) -> None:
        if self._focus_target() != "projects":
            return
        project = self._selected_project()
        if project is None:
            return
        if not project.active:
            self.store.update_project(project.id, active=True)
            await self._rebuild_stage()
        await self._start_project(project)
        self._refresh_projects()
        self._refresh_details()

    async def action_stop_session(self) -> None:
        if self._focus_target() != "projects":
            return
        project = self._selected_project()
        if project is None:
            return
        await self._stop_project(project.id)
        self._refresh_projects()
        self._refresh_details()

    async def action_restart_session(self) -> None:
        """Restart the selected session -- also how you abort a runaway turn."""
        project = self._selected_project() or self.config.project(self._selected)
        if project is None:
            return
        widget = self._stages.get(project.id)
        if isinstance(widget, ProjectPane):
            widget.write_banner("-- restarting session --")
        if self.interactive:
            await self.terminals.ensure(project).restart()
        else:
            await self.sessions.ensure(project).restart()
        self._refresh_projects()
        self._refresh_details()

    async def action_toggle_pane_mode(self) -> None:
        """Switch between a real console and a headless log."""
        if not terminal.AVAILABLE:
            self.notify(
                "Interactive consoles need pywinpty and pyte:\n"
                "  .\\.venv\\Scripts\\python.exe -m pip install pywinpty pyte",
                title="Staying in headless mode",
                severity="warning",
                timeout=12,
            )
            return
        going_interactive = self.config.pane_mode != "interactive"
        # Nothing is torn down here.  Killing the consoles would throw away their
        # scrollback -- a new ConPTY gets a blank emulator screen, so switching back
        # would lose everything on screen even though `--continue` restores the
        # conversation.  Instead the sessions keep running unseen and switching back
        # re-attaches the same TerminalSession, screen and all.  This is safe because an
        # idle headless shell does not run `claude` at all (it only does so per prompt),
        # so the two modes cannot both drive Claude unless a prompt is actually sent --
        # which _conflicting_console() below refuses.
        self.store.set_pane_mode("interactive" if going_interactive else "headless")
        await self._rebuild_stage()
        self._refresh_projects()
        self._refresh_details()
        self.notify(
            "Real console — history preserved."
            if going_interactive
            else "Headless log. Press f10 to go back; your consoles keep running.",
            title=f"Pane mode: {self.config.pane_mode}",
        )
        if self.config.autostart:
            self.run_worker(self._autostart(), exclusive=False)

    def _conflicting_console(self, project: Project) -> bool:
        """True (and warns) when a live interactive console owns this conversation.

        Sending a headless prompt while that project's real console is up would put two
        Claude processes on the same conversation, so the headless side steps aside.
        """
        session = self.terminals.get(project.id)
        if session is None or not session.is_running:
            return False
        self.notify(
            f"'{project.name}' has a live console with its own Claude session.\n"
            "Press f10 to switch back to it, or stop it first (f2 then x).",
            title="Not sending from the headless log",
            severity="warning",
            timeout=12,
        )
        return True

    # ---------------------------------------------------------- task actions --

    @work
    async def _add_task(self) -> None:
        result = await self.push_screen_wait(TaskForm(self.config.projects))
        if not result:
            return
        self.store.add_task(Task(**result))
        self._refresh_tasks()
        self._refresh_details()

    @work
    async def _edit_task(self) -> None:
        task = self._selected_task()
        if task is None:
            return
        result = await self.push_screen_wait(TaskForm(self.config.projects, task))
        if not result:
            return
        self.store.update_task(task.id, **result)
        self._refresh_tasks()

    @work
    async def _delete_task(self) -> None:
        task = self._selected_task()
        if task is None:
            return
        confirmed = await self.push_screen_wait(ConfirmDialog(f"Delete task '{task.title}'?"))
        if confirmed:
            self.store.delete_task(task.id)
            self._refresh_tasks()
            self._refresh_details()

    def action_move_task(self, direction: int) -> None:
        if self._focus_target() != "tasks":
            return
        task = self._selected_task()
        if task is None:
            return
        self.store.move_task(task.id, direction)
        self._refresh_tasks()
        self._refresh_details()

    def action_complete_task(self) -> None:
        if self._focus_target() != "tasks":
            return
        task = self._selected_task()
        if task is None:
            return
        self.store.update_task(task.id, column="completed")
        self._refresh_tasks()
        self._refresh_details()

    async def action_send_task(self) -> None:
        """Hand the selected TODO to its project's Claude session."""
        if self._focus_target() != "tasks":
            return
        task = self._selected_task()
        if task is None:
            return
        project = self.config.project(task.project_id)
        if project is None:
            self.notify("This task has no linked project -- press e to pick one.",
                        severity="warning")
            return
        if not project.active:
            self.store.update_project(project.id, active=True)
            await self._rebuild_stage()
            self._refresh_projects()

        prompt = task.title if not task.notes.strip() else f"{task.title}\n\n{task.notes}"
        if self.interactive:
            session = self.terminals.ensure(project)
            if not session.is_running:
                session.start()
                await asyncio.sleep(2.5)  # let PowerShell + claude come up
            sent = session.send_text(prompt)
        else:
            if self._conflicting_console(project):
                return
            sent = await self.sessions.ensure(project).send_prompt(prompt)

        if not sent:
            self.notify(f"Could not send to '{project.name}'.", severity="error")
            return
        self.store.update_task(task.id, column="in_progress")
        self._refresh_tasks()
        self._select_project(project.id)
        self.action_focus_console()
        self.notify(f"Sent to '{project.name}'.", title="Assigned to Claude")

    # ------------------------------------------------- shared verb dispatch --

    def action_add(self) -> None:
        target = self._focus_target()
        if target == "projects":
            self._add_project()
        elif target == "tasks":
            self._add_task()

    def action_edit(self) -> None:
        target = self._focus_target()
        if target == "projects":
            self._edit_project()
        elif target == "tasks":
            self._edit_task()

    def action_delete(self) -> None:
        target = self._focus_target()
        if target == "projects":
            self._delete_project()
        elif target == "tasks":
            self._delete_task()

    # -------------------------------------------------------------- teardown --

    async def _teardown(self) -> None:
        """Kill every background console/shell and everything inside them."""
        if self._tearing_down:
            return
        self._tearing_down = True
        logs.log().info("shutting down")
        await self.terminals.stop_all()
        await self.sessions.stop_all()

    async def action_quit(self) -> None:  # type: ignore[override]
        await self._teardown()
        self.exit()

    async def on_unmount(self) -> None:
        # Safety net for exits that bypass action_quit; process.py also has an atexit
        # hook, and every session lives in a kill-on-close job object.
        await self._teardown()


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vyctl",
        description="Terminal dashboard for parallel Claude Code sessions.",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="path to config.json (default: alongside the application)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="write a debug log next to config.json (overrides log_level)",
    )
    parser.add_argument(
        "--debug-raw",
        action="store_true",
        help="also dump raw console output to that log (contains your session text)",
    )
    parser.add_argument("--version", action="version", version=f"vyctl {__version__}")
    args = parser.parse_args(argv)

    if sys.platform == "win32":
        # The Proactor loop is required for subprocess pipes on Windows; it is already
        # the default on 3.8+, but set it explicitly so a host policy cannot break us.
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    config_path = str(Path(args.config).expanduser()) if args.config else None
    app = VyctlApp(config_path=config_path)
    if args.debug or args.debug_raw:
        # Re-configure with the CLI override; the app already logged its startup line.
        app.log_path = logs.setup(
            app.store.path.parent, level="debug", raw_stream=args.debug_raw
        )
    app.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
