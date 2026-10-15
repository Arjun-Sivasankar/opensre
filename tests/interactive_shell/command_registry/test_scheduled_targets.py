"""Searchable target selection for provider-specific scheduled actions."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from prompt_toolkit.application import create_app_session
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
