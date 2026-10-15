"""Searchable target selection for provider-specific scheduled actions."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from config.command_inputs import needs_command_input
from infrastructure.scheduling.scheduler.storage import task_store
from infrastructure.scheduling.scheduler.types import Provider, ScheduledTask, TaskKind
from surfaces.interactive_shell.command_registry import (
    cli_parity,
    dispatch_slash,
    input_collection,
    scheduled_targets,
)
from surfaces.interactive_shell.runtime import input_policy
from surfaces.interactive_shell.session import Session
from surfaces.interactive_shell.ui import scheduled_target
from surfaces.interactive_shell.ui.scheduled_target import build_scheduled_task_picker


@pytest.fixture(autouse=True)
def isolated_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        task_store, "default_task_store_path", lambda: tmp_path / "scheduler" / "tasks.json"
    )
    monkeypatch.setattr(input_policy, "repl_tty_interactive", lambda: True)
    monkeypatch.setattr(input_collection, "repl_tty_interactive", lambda: True)


def run_keys(app: Any, keys: str) -> Any:
    with create_pipe_input() as pipe:
        app.input = pipe
        app.output = DummyOutput()
        return app.run(pre_run=lambda: pipe.send_text(keys))


def dispatch(command: str, session: Session | None = None) -> Session:
    session = session or Session()
    session.terminal.exclusive_stdin_active = input_policy.turn_needs_exclusive_stdin(
        command, session
    )
    dispatch_slash(command, session, Console(file=io.StringIO(), width=90), is_tty=True)
    return session


def seed_tasks() -> tuple[ScheduledTask, ScheduledTask, ScheduledTask]:
    tasks = (
        ScheduledTask(
            id="sentry-digest-task",
            kind=TaskKind.SENTRY_MORNING_DIGEST,
            cron="0 8 * * mon-fri",
            timezone="Europe/London",
            provider=Provider.SLACK,
            chat_id="C-sentry",
            params={"project_slug": "payments"},
        ),
        ScheduledTask(
            id="sentry-uptime-task",
            kind=TaskKind.SENTRY_UPTIME_WATCH,
            cron="*/5 * * * *",
            provider=Provider.TELEGRAM,
            chat_id="-100-uptime",
        ),
        ScheduledTask(
            id="posthog-report-task",
            kind=TaskKind.POSTHOG_METRIC_REPORT,
            cron="0 9 * * mon",
            provider=Provider.ROCKETCHAT,
            chat_id="#metrics",
            params={"stats_period": "7d", "metrics": "active users,pageviews"},
        ),
    )
    path = task_store.default_task_store_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([task.model_dump(mode="json") for task in tasks]))
    return tasks


@pytest.mark.parametrize(("columns", "rows"), [(40, 12), (40, 24), (90, 30)])
def test_scheduled_picker_review_preserves_full_identity_and_timezone(
    columns: int, rows: int
) -> None:
    class SizedOutput(DummyOutput):
        def get_size(self) -> Size:
            return Size(rows=rows, columns=columns)

    task = ScheduledTask(
        id="12345678-1234-1234-1234-123456789012",
        name="Payments [literal] 日本 digest",
        kind=TaskKind.SENTRY_MORNING_DIGEST,
        cron="0 8 * * mon-fri",
        timezone="Asia/Kolkata",
        provider=Provider.SLACK,
        chat_id="C-payments",
    )
    captured: list[str] = []
    with create_pipe_input() as pipe, create_app_session(output=SizedOutput()):
        app = build_scheduled_task_picker(
            [task],
            command="/sentry digest schedule run",
            action_label="Run now",
            confirmation="Run this scheduled task now with its configured delivery?",
            compact_confirmation="Run with configured delivery?",
        )
        app.input = pipe

        def capture(_app: Any) -> None:
            screen = app.renderer.last_rendered_screen
            if screen is None or captured:
                return
            lines = [
                "".join(screen.data_buffer[y][x].char for x in range(columns))
                for y in range(screen.height)
            ]
            if not any("Cancel" in line for line in lines):
                return
            captured.extend(lines)
            pipe.send_text("\x03")

        app.after_render += capture
        assert app.run(pre_run=lambda: pipe.send_text("\r")) is None

    content = "".join(line.strip(" │") for line in captured)
    assert task.id in content
    assert "<id>" not in content
    assert task.timezone in content
    assert "[literal]" in content and "日本" in content
    assert "› Run now" in content and "Cancel" in content


def test_scheduled_picker_keeps_full_id_outside_summary_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = ScheduledTask(
        id="full-task-id-that-must-not-affect-summary-column-widths",
        name="Payments digest",
        kind=TaskKind.SENTRY_MORNING_DIGEST,
        cron="0 8 * * mon-fri",
        timezone="Europe/London",
        provider=Provider.SLACK,
        chat_id="C-payments",
    )
    captured: dict[str, Any] = {}

    def build(choices: Any, **kwargs: Any) -> Any:
        captured["choices"] = choices
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(scheduled_target, "build_target_picker", build)
    scheduled_target.build_scheduled_task_picker(
        [task],
        command="/sentry digest schedule remove",
        action_label="Remove permanently",
    )

    choice = captured["choices"][0]
    assert [column.header for column in captured["columns"]] == [
        "Task",
        "Destination",
        "Schedule",
        "TZ",
        "State",
    ]
    assert len(choice.cells) == 4
    assert choice.metadata[0].plain == f"Full task ID: {task.id}"


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("/sentry", ["digest", "schedule", "run"]),
        ("/sentry", ["digest", "schedule", "remove"]),
        ("/sentry", ["uptime", "watch", "run"]),
        ("/sentry", ["uptime", "watch", "remove"]),
        ("/posthog", ["report", "schedule", "run"]),
        ("/posthog", ["report", "schedule", "remove"]),
    ],
)
def test_nested_schedule_inputs_only_match_missing_targets(command: str, args: list[str]) -> None:
    assert needs_command_input(command, args)
    assert input_policy.turn_needs_exclusive_stdin(f"{command} {' '.join(args)}", Session())
    assert not needs_command_input(command, [*args, "task-id"])
    assert not input_policy.turn_needs_exclusive_stdin(
        f"{command} {' '.join([*args, 'task-id'])}", Session()
    )


def test_sentry_remove_picker_delegates_exact_task_id(monkeypatch: pytest.MonkeyPatch) -> None:
    digest, _uptime, _posthog = seed_tasks()
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def collect(app: Any) -> Any:
        return run_keys(app, "payments\r\r")

    def run_cli(_console: Console, args: list[str], **kwargs: Any) -> bool:
        calls.append((args, kwargs))
        return True

    monkeypatch.setattr(scheduled_targets, "run_command_input", collect)
    monkeypatch.setattr(cli_parity, "run_cli_command", run_cli)
    with create_app_session(output=DummyOutput()):
        dispatch("/sentry digest schedule remove")

    assert len(calls) == 1
    assert calls[0][0] == [
        "sentry",
        "digest",
        "schedule",
        "remove",
        digest.id,
    ]


def test_posthog_run_picker_queues_exact_task_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _digest, _uptime, posthog = seed_tasks()
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def collect(app: Any) -> Any:
        return run_keys(app, "7d\r\r")

    def run_cli(_console: Console, args: list[str], **kwargs: Any) -> bool:
        calls.append((args, kwargs))
        return True

    monkeypatch.setattr(scheduled_targets, "run_command_input", collect)
    monkeypatch.setattr(cli_parity, "run_cli_command", run_cli)
    session = Session()
    with create_app_session(output=DummyOutput()):
        dispatch("/posthog report schedule run", session)

    assert calls == []
    queued = session.terminal.pop_pending_prompt_default()
    assert queued == f"/posthog report schedule run {posthog.id}"
    assert not input_policy.turn_needs_exclusive_stdin(queued, session)

    dispatch(queued, session)
    assert calls[0][0] == [
        "posthog",
        "report",
        "schedule",
        "run",
        posthog.id,
    ]
    assert calls[0][1]["capture_output"] is True


def test_explicit_sentry_target_bypasses_picker(monkeypatch: pytest.MonkeyPatch) -> None:
    _digest, uptime, _posthog = seed_tasks()
    calls: list[list[str]] = []

    def unexpected(_app: Any) -> Any:
        raise AssertionError("An explicit target must not open a picker")

    def run_cli(_console: Console, args: list[str], **_kwargs: Any) -> bool:
        calls.append(args)
        return True

    monkeypatch.setattr(scheduled_targets, "run_command_input", unexpected)
    monkeypatch.setattr(cli_parity, "run_cli_command", run_cli)
    dispatch(f"/sentry uptime watch remove {uptime.id}")

    assert calls == [["sentry", "uptime", "watch", "remove", uptime.id]]
