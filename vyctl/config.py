"""
Configuration model + persistence for vyctl.

Everything the user configures (projects, kanban tasks, dashboard layout) lives in a
single human-editable ``config.json`` in the application directory.  The file is written
atomically (temp file + ``os.replace``) so a crash mid-save can never leave a truncated
config behind.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

#: Kanban column identifiers (Tab 3).  Order matters -- it defines left-to-right order
#: and therefore which way "move task" travels.
COLUMNS: tuple[str, ...] = ("backlog", "in_progress", "completed")

#: Human readable column titles.
COLUMN_TITLES: dict[str, str] = {
    "backlog": "Backlog",
    "in_progress": "In Progress (Claude)",
    "completed": "Completed",
}

#: Supported dashboard layouts (Tab 1).
LAYOUTS: tuple[str, ...] = ("grid", "vertical")

#: Supported dashboard pane modes (Tab 1).
PANE_MODES: tuple[str, ...] = ("interactive", "headless")


def new_id() -> str:
    """Short, collision-safe identifier used for projects and tasks."""
    return uuid.uuid4().hex[:8]


def _now() -> str:
    """Current local time as an ISO-8601 string (stored in config.json)."""
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------------------
# Dataclasses
# --------------------------------------------------------------------------------------


class _JsonModel:
    """Mixin giving dataclasses tolerant ``from_dict`` / ``to_dict`` behaviour.

    Unknown keys in ``config.json`` are ignored rather than raising, so a config written
    by a newer version of vyctl still loads (forward compatibility).
    """

    @classmethod
    def from_dict(cls, data: dict[str, Any]):
        known = {f.name for f in fields(cls)}  # type: ignore[arg-type]
        return cls(**{k: v for k, v in data.items() if k in known})  # type: ignore[call-arg]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)  # type: ignore[call-overload]


@dataclass
class Project(_JsonModel):
    """A single configured project == one background PowerShell + Claude session."""

    name: str
    path: str
    #: Base Claude Code invocation.  Defaults to ``claude``; may include flags such as
    #: ``claude --model opus``.  vyctl appends the print-mode/session flags itself.
    command: str = "claude"
    #: Extra CLI arguments appended to every invocation, e.g.
    #: ``--permission-mode acceptEdits`` or ``--add-dir ../shared``.
    extra_args: str = ""
    #: Inactive projects are still listed but no process is spawned for them.
    active: bool = True
    id: str = field(default_factory=new_id)

    @property
    def resolved_path(self) -> Path:
        """Project path with ``~`` and ``%VAR%`` expanded."""
        return Path(os.path.expandvars(self.path)).expanduser()

    def path_exists(self) -> bool:
        try:
            return self.resolved_path.is_dir()
        except OSError:  # malformed path on Windows (e.g. illegal characters)
            return False


@dataclass
class Task(_JsonModel):
    """A kanban card on the Workspace Planner tab."""

    title: str
    column: str = "backlog"
    #: ``Project.id`` this task is assigned to, or ``None`` when unassigned.
    project_id: str | None = None
    notes: str = ""
    id: str = field(default_factory=new_id)
    created: str = field(default_factory=_now)
    updated: str = field(default_factory=_now)

    def touch(self) -> None:
        self.updated = _now()


@dataclass
class AppConfig(_JsonModel):
    """Root config object -- the whole of ``config.json``."""

    projects: list[Project] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    #: Dashboard pane arrangement: ``grid`` (2 columns) or ``vertical`` (1 column).
    layout: str = "grid"
    #: How each dashboard pane runs Claude:
    #:   "interactive" -- a real ConPTY per project running the ordinary `claude` TUI,
    #:                    exactly as it looks in your own terminal (needs pywinpty+pyte);
    #:   "headless"    -- pipe-driven `claude --print` turns rendered as a clean log.
    pane_mode: str = "interactive"
    #: When true the first prompt of a session carries ``--continue`` so Claude picks up
    #: the most recent conversation in that folder instead of starting cold.
    auto_resume: bool = True
    #: Spawn processes for every active project as soon as the app starts.
    autostart: bool = True
    #: Opt-in: on startup, actually run one tiny resumed Claude turn per project so the
    #: previous conversation is verifiably re-attached and its session id is known.
    #: Off by default because it costs a (small) real API turn per project per launch.
    resume_probe: bool = False
    #: Parse ``--output-format stream-json`` for incremental live output.  Turning this
    #: off falls back to plain text (output only appears once a turn finishes).
    stream_json: bool = True
    #: Lines of scrollback retained per pane.
    max_log_lines: int = 2000
    #: Launch embedded consoles with Claude Code's *classic* renderer.  Its fullscreen
    #: renderer uses the alternate screen buffer, which the terminal emulator does not
    #: model -- the symptom is a pane where everything looks highlighted.  Passed per
    #: launch via ``--settings``, so your own ~/.claude/settings.json is never touched.
    force_classic_tui: bool = True
    #: Alert when a background session finishes a turn or asks for approval.  Running
    #: several sessions only pays off if you do not have to watch them, so this is on.
    notify_on_idle: bool = True
    #: Ring the terminal bell alongside the in-app toast.  Windows Terminal turns a bell
    #: into a taskbar flash when the profile sets bellStyle, which is the point.
    notify_bell: bool = True
    #: Lines of scrollback each interactive console keeps.  Without history a terminal
    #: emulator holds only the visible rows, so there is nothing to scroll back to.
    scrollback_lines: int = 5000
    #: Debug log verbosity: "off", "info" or "debug".  Writes vyctl.log next to
    #: this config file.
    log_level: str = "off"
    #: Also dump every byte the consoles emit into that log.  Invaluable for chasing a
    #: rendering bug, but it contains the full text of your sessions -- opt in, then off.
    log_raw_stream: bool = False

    # -- lookups ----------------------------------------------------------------

    def project(self, project_id: str | None) -> Project | None:
        if project_id is None:
            return None
        return next((p for p in self.projects if p.id == project_id), None)

    def task(self, task_id: str) -> Task | None:
        return next((t for t in self.tasks if t.id == task_id), None)

    def active_projects(self) -> list[Project]:
        return [p for p in self.projects if p.active]

    def tasks_in(self, column: str) -> list[Task]:
        return [t for t in self.tasks if t.column == column]

    def project_name(self, project_id: str | None) -> str:
        project = self.project(project_id)
        return project.name if project else "unassigned"

    # -- (de)serialisation ------------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppConfig:
        known = {f.name for f in fields(cls)}
        payload = {k: v for k, v in data.items() if k in known}
        payload["projects"] = [Project.from_dict(p) for p in data.get("projects", [])]
        payload["tasks"] = [Task.from_dict(t) for t in data.get("tasks", [])]
        if payload.get("layout") not in LAYOUTS:
            payload["layout"] = "grid"
        if payload.get("pane_mode") not in PANE_MODES:
            payload["pane_mode"] = "interactive"
        config = cls(**payload)
        # Repair dangling references: cards pointing at deleted projects, bad columns.
        valid_ids = {p.id for p in config.projects}
        for task in config.tasks:
            if task.column not in COLUMNS:
                task.column = "backlog"
            if task.project_id not in valid_ids:
                task.project_id = None
        return config

    def to_dict(self) -> dict[str, Any]:
        return {
            "projects": [p.to_dict() for p in self.projects],
            "tasks": [t.to_dict() for t in self.tasks],
            "layout": self.layout,
            "pane_mode": self.pane_mode,
            "auto_resume": self.auto_resume,
            "autostart": self.autostart,
            "resume_probe": self.resume_probe,
            "stream_json": self.stream_json,
            "max_log_lines": self.max_log_lines,
            "force_classic_tui": self.force_classic_tui,
            "notify_on_idle": self.notify_on_idle,
            "notify_bell": self.notify_bell,
            "scrollback_lines": self.scrollback_lines,
            "log_level": self.log_level,
            "log_raw_stream": self.log_raw_stream,
        }


# --------------------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------------------


class ConfigStore:
    """Loads and atomically saves :class:`AppConfig` to a JSON file.

    Every mutation helper persists immediately, which is what the spec asks for:
    "changes must immediately update the config.json file".
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        if path is None:
            env = os.environ.get("VYCTL_CONFIG")
            path = env if env else Path(__file__).resolve().parent.parent / "config.json"
        self.path = Path(path)
        self.config = AppConfig()
        #: Populated with a human readable message when the last load failed.
        self.load_error: str | None = None
        #: True when ``load()`` found no config file (first run).
        self.is_first_run = False

    def load(self) -> AppConfig:
        """Read the config file.  A missing or corrupt file yields safe defaults."""
        self.load_error = None
        self.is_first_run = not self.path.exists()
        if self.is_first_run:
            self.config = AppConfig()
            return self.config
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("top-level JSON value is not an object")
            self.config = AppConfig.from_dict(raw)
        except Exception as exc:  # a corrupt config must never block startup
            self.load_error = f"{self.path.name}: {exc}"
            # Preserve the unreadable file so nothing is silently destroyed.
            backup = self.path.with_suffix(".json.bak")
            try:
                backup.write_bytes(self.path.read_bytes())
                self.load_error += f" (backed up to {backup.name})"
            except OSError:
                pass
            self.config = AppConfig()
        return self.config

    def save(self) -> None:
        """Write the config file atomically (temp file + replace)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.config.to_dict(), indent=2, ensure_ascii=False)
        handle, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".config-", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload + "\n")
            os.replace(tmp_name, self.path)  # atomic on Windows and POSIX
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # -- project mutations ------------------------------------------------------

    def add_project(self, project: Project) -> Project:
        self.config.projects.append(project)
        self.save()
        return project

    def update_project(self, project_id: str, **changes: Any) -> Project | None:
        project = self.config.project(project_id)
        if project is None:
            return None
        for key, value in changes.items():
            if hasattr(project, key):
                setattr(project, key, value)
        self.save()
        return project

    def delete_project(self, project_id: str) -> bool:
        before = len(self.config.projects)
        self.config.projects = [p for p in self.config.projects if p.id != project_id]
        # Unlink any cards that pointed at the removed project.
        for task in self.config.tasks:
            if task.project_id == project_id:
                task.project_id = None
        if len(self.config.projects) != before:
            self.save()
            return True
        return False

    # -- task mutations ---------------------------------------------------------

    def add_task(self, task: Task) -> Task:
        self.config.tasks.append(task)
        self.save()
        return task

    def update_task(self, task_id: str, **changes: Any) -> Task | None:
        task = self.config.task(task_id)
        if task is None:
            return None
        for key, value in changes.items():
            if hasattr(task, key):
                setattr(task, key, value)
        task.touch()
        self.save()
        return task

    def delete_task(self, task_id: str) -> bool:
        before = len(self.config.tasks)
        self.config.tasks = [t for t in self.config.tasks if t.id != task_id]
        if len(self.config.tasks) != before:
            self.save()
            return True
        return False

    def move_task(self, task_id: str, direction: int) -> Task | None:
        """Shift a card one column left (``-1``) or right (``+1``)."""
        task = self.config.task(task_id)
        if task is None:
            return None
        index = COLUMNS.index(task.column) if task.column in COLUMNS else 0
        target = min(max(index + direction, 0), len(COLUMNS) - 1)
        if target != index:
            task.column = COLUMNS[target]
            task.touch()
            self.save()
        return task

    # -- misc -------------------------------------------------------------------

    def set_layout(self, layout: str) -> None:
        if layout in LAYOUTS:
            self.config.layout = layout
            self.save()

    def set_pane_mode(self, mode: str) -> None:
        if mode in PANE_MODES:
            self.config.pane_mode = mode
            self.save()

    def seed_example(self, paths: Iterable[str]) -> None:
        """Create starter projects (used only on first run)."""
        for path in paths:
            name = Path(path).name or path
            self.config.projects.append(Project(name=name, path=str(path)))
        self.save()
