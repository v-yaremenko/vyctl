"""config.json is the user's data: projects, kanban cards and every setting."""

import json

from vyctl.config import COLUMNS, AppConfig, Project, Task


def test_roundtrip_preserves_projects_and_tasks():
    cfg = AppConfig(
        projects=[Project(name="A", path="C:\a", id="p1")],
        tasks=[Task(title="do it", column="in_progress", project_id="p1", id="t1")],
    )
    again = AppConfig.from_dict(json.loads(json.dumps(cfg.to_dict())))
    assert [p.name for p in again.projects] == ["A"]
    assert again.tasks[0].title == "do it"
    assert again.tasks[0].column == "in_progress"
    assert again.tasks[0].project_id == "p1"


def test_unknown_keys_are_dropped_not_fatal():
    """A config written by a newer version must not crash an older one."""
    cfg = AppConfig.from_dict({"projects": [], "tasks": [], "some_future_setting": 42})
    assert cfg.projects == []


def test_bad_column_falls_back_to_backlog():
    cfg = AppConfig.from_dict({"tasks": [{"title": "x", "column": "nonsense"}]})
    assert cfg.tasks[0].column == "backlog"
    assert cfg.tasks[0].column in COLUMNS


def test_task_pointing_at_a_deleted_project_is_unassigned():
    cfg = AppConfig.from_dict(
        {"projects": [], "tasks": [{"title": "orphan", "project_id": "gone"}]}
    )
    assert cfg.tasks[0].project_id is None


def test_invalid_layout_and_pane_mode_are_repaired():
    cfg = AppConfig.from_dict({"layout": "spiral", "pane_mode": "telepathy"})
    assert cfg.layout == "grid"
    assert cfg.pane_mode == "interactive"


def test_empty_config_is_valid():
    cfg = AppConfig.from_dict({})
    assert cfg.projects == [] and cfg.tasks == []
