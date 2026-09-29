import json
import sqlite3
from pathlib import Path

import pytest

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import antigravity, antigravity_desktop
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

TARGET_ID = "33333333-3333-4333-8333-333333333333"
TRAJECTORY_ID = "44444444-4444-4444-8444-444444444444"


def portable_session(tmp_path: Path) -> Session:
    events = (
        Event(
            kind=EventKind.MESSAGE,
            role=Role.USER,
            text="AGY_DESKTOP_USER_ALPHA",
            timestamp="2026-08-20T12:00:00Z",
            provenance=Provenance(0, "user"),
        ),
        Event(
            kind=EventKind.MESSAGE,
            role=Role.ASSISTANT,
            text="AGY_DESKTOP_ASSISTANT_OMEGA",
            timestamp="2026-08-20T12:00:01Z",
            provenance=Provenance(1, "assistant"),
        ),
        Event(
            kind=EventKind.TOOL_CALL,
            role=Role.ASSISTANT,
            tool_name="echo_marker",
            tool_call_id="agy-call-1",
            timestamp="2026-08-20T12:00:02Z",
            payload={"input": {"text": "DESKTOP_INPUT_ALPHA", "count": 2}},
            provenance=Provenance(2, "tool_call"),
        ),
        Event(
            kind=EventKind.TOOL_RESULT,
            role=Role.TOOL,
            text="DESKTOP_RESULT_OMEGA",
            tool_name="echo_marker",
            tool_call_id="agy-call-1",
            timestamp="2026-08-20T12:00:03Z",
            payload={
                "is_error": False,
                "content_blocks": [{"type": "text", "text": "DESKTOP_RESULT_OMEGA"}],
            },
            provenance=Provenance(3, "tool_result"),
        ),
        Event(
            kind=EventKind.THINKING,
            role=Role.ASSISTANT,
            text="PRIVATE_THINKING_MUST_NOT_SURVIVE",
            provenance=Provenance(4, "thinking"),
        ),
    )
    return Session(
        source_format=AgentFormat.CLAUDE,
        source_path=tmp_path / "source.jsonl",
        source_sha256="0" * 64,
        session_id="11111111-1111-4111-8111-111111111111",
        cwd=tmp_path,
        started_at="2026-08-20T12:00:00Z",
        cli_version="2.1.209",
        model="fixture-model",
        title="Antigravity Desktop fixture",
        events=events,
        raw_record_count=len(events),
    )


def event_signature(events: tuple[Event, ...]) -> list[tuple[object, ...]]:
    result = []
    for event in events:
        if event.kind == EventKind.THINKING:
            continue
        payload = event.payload.get("input") if event.kind == EventKind.TOOL_CALL else None
        result.append(
            (
                event.kind,
                event.role,
                event.text,
                event.tool_name,
                event.tool_call_id,
                json.dumps(payload, sort_keys=True),
            )
        )
    return result


def test_desktop_round_trip_preserves_messages_and_tools(tmp_path: Path) -> None:
    source = portable_session(tmp_path)
    data, dropped = antigravity_desktop.serialize(
        source,
        session_id=TARGET_ID,
        trajectory_id=TRAJECTORY_ID,
        cwd=tmp_path,
        timestamp="2026-08-20T12:00:00Z",
    )
    path = tmp_path / f"{TARGET_ID}.db"
    path.write_bytes(data)

    antigravity_desktop.validate_native_bytes(data, TARGET_ID)
    parsed = antigravity_desktop.parse(path)

    assert dropped == {"thinking:private": 1}
    assert b"PRIVATE_THINKING_MUST_NOT_SURVIVE" not in data
    assert antigravity_desktop.native_record_count(data) == 4
    assert event_signature(parsed.events) == event_signature(source.events)

    # Verify Desktop wire differences
    with sqlite3.connect(path) as conn:
        meta = conn.execute(
            "SELECT trajectory_id, cascade_id, trajectory_type, source FROM trajectory_meta"
        ).fetchone()
        assert meta == (TRAJECTORY_ID, TARGET_ID, 4, 1)  # Desktop source is 1, not 17!


def test_desktop_parse_session_attributes(tmp_path: Path) -> None:
    source = portable_session(tmp_path)
    data, _ = antigravity_desktop.serialize(
        source,
        session_id=TARGET_ID,
        trajectory_id=TRAJECTORY_ID,
        cwd=tmp_path,
    )
    path = tmp_path / f"{TARGET_ID}.db"
    path.write_bytes(data)

    session = antigravity_desktop.parse_session(path)
    assert session.source_format == AgentFormat.ANTIGRAVITY_DESKTOP
    assert session.session_id == TARGET_ID
    assert session.model_provider == "google"
    assert session.cli_version == antigravity_desktop.PINNED_ANTIGRAVITY_DESKTOP_VERSION
    assert len(session.events) == 4


def test_desktop_installation_updates_summary_database(tmp_path: Path) -> None:
    source = portable_session(tmp_path)
    data, _ = antigravity_desktop.serialize(
        source,
        session_id=TARGET_ID,
        trajectory_id=TRAJECTORY_ID,
        cwd=tmp_path,
        timestamp="2026-08-20T12:00:00Z",
    )
    target_home = tmp_path / "antigravity"

    # Dry run check
    installed_dry = antigravity_desktop.install_database(
        data,
        session_id=TARGET_ID,
        cwd=tmp_path,
        timestamp="2026-08-20T12:00:00Z",
        title="Test Desktop Session",
        target_home=target_home,
        dry_run=True,
    )
    assert not installed_dry.conversation_path.exists()
    assert not installed_dry.summaries_path.exists()

    # Actual installation
    installed = antigravity_desktop.install_database(
        data,
        session_id=TARGET_ID,
        cwd=tmp_path,
        timestamp="2026-08-20T12:00:00Z",
        title="Test Desktop Session",
        target_home=target_home,
        dry_run=False,
    )
    assert installed.conversation_path.is_file()
    assert installed.summaries_path.is_file()

    # Verify summary columns
    with sqlite3.connect(installed.summaries_path) as conn:
        row = conn.execute(
            "SELECT conversation_id, title, status, source, project_id, app_data_dir "
            "FROM conversation_summaries WHERE conversation_id = ?",
            (TARGET_ID,),
        ).fetchone()
        assert row == (
            TARGET_ID,
            "Test Desktop Session",
            "CASCADE_RUN_STATUS_IDLE",
            "",
            "outside-of-project",
            "antigravity",
        )

    # Re-installation collision check
    with pytest.raises(SessionMigrateError, match="already exists"):
        antigravity_desktop.install_database(
            data,
            session_id=TARGET_ID,
            cwd=tmp_path,
            timestamp="2026-08-20T12:00:00Z",
            title="Duplicate",
            target_home=target_home,
            dry_run=False,
        )


def test_desktop_and_cli_databases_do_not_falsely_detect_each_other(tmp_path: Path) -> None:
    source = portable_session(tmp_path)
    cli_data, _ = antigravity.serialize(
        source,
        session_id=TARGET_ID,
        trajectory_id=TRAJECTORY_ID,
        cwd=tmp_path,
    )
    desktop_data, _ = antigravity_desktop.serialize(
        source,
        session_id=TARGET_ID,
        trajectory_id=TRAJECTORY_ID,
        cwd=tmp_path,
    )

    cli_path = tmp_path / "cli.db"
    desktop_path = tmp_path / "desktop.db"
    cli_path.write_bytes(cli_data)
    desktop_path.write_bytes(desktop_data)

    # CLI parser rejects desktop DB
    with pytest.raises(SessionMigrateError, match="CLI cascade trajectory"):
        antigravity.parse(desktop_path)

    # Desktop parser rejects CLI DB
    with pytest.raises(SessionMigrateError, match="Desktop cascade trajectory"):
        antigravity_desktop.parse(cli_path)


def test_desktop_handles_cleared_step(tmp_path: Path) -> None:
    # A compacted conversation can contain step status 5 (CLEARED) with minimal payload
    data, _ = antigravity_desktop.serialize(
        portable_session(tmp_path),
        session_id=TARGET_ID,
        trajectory_id=TRAJECTORY_ID,
        cwd=tmp_path,
    )
    path = tmp_path / f"{TARGET_ID}.db"
    path.write_bytes(data)

    # Inject a CLEARED step
    with sqlite3.connect(path) as conn:
        cleared_payload = antigravity._field_varint(
            1, antigravity.STEP_TYPE_PLANNER_RESPONSE
        ) + antigravity._field_varint(4, 5)
        conn.execute(
            "INSERT INTO steps(idx, step_type, status, has_subtrajectory, metadata, "
            "step_payload, step_format) VALUES(?, ?, 5, 0, NULL, ?, 0)",
            (4, antigravity.STEP_TYPE_PLANNER_RESPONSE, cleared_payload),
        )

    parsed = antigravity_desktop.parse(path)
    assert any(
        event.kind == EventKind.OPAQUE and event.payload.get("reason") == "antigravity_cleared_step"
        for event in parsed.events
    )


def test_format_detection_identifies_desktop(tmp_path: Path) -> None:
    from session_migrate.inspection import detect_path_format

    data, _ = antigravity_desktop.serialize(
        portable_session(tmp_path),
        session_id=TARGET_ID,
        trajectory_id=TRAJECTORY_ID,
        cwd=tmp_path,
    )
    path = tmp_path / f"{TARGET_ID}.db"
    path.write_bytes(data)

    detected = detect_path_format(path)
    assert detected == AgentFormat.ANTIGRAVITY_DESKTOP


def test_conversion_pipeline_cli_to_desktop_and_back(tmp_path: Path) -> None:
    from session_migrate.conversion import (
        ConversionOptions,
        convert_session,
        install_antigravity_desktop_artifact,
        load_session,
    )
    from session_migrate.model import TargetFormat

    # 1. Start with a CLI session
    cli_data, _ = antigravity.serialize(
        portable_session(tmp_path),
        session_id=TARGET_ID,
        trajectory_id=TRAJECTORY_ID,
        cwd=tmp_path,
        timestamp="2026-08-20T12:00:00Z",
    )
    cli_path = tmp_path / f"{TARGET_ID}.db"
    cli_path.write_bytes(cli_data)
    loaded_cli = load_session(cli_path)
    assert loaded_cli.source_format == AgentFormat.ANTIGRAVITY

    # 2. Convert CLI -> Desktop
    desktop_id = "55555555-5555-4555-8555-555555555555"
    artifact = convert_session(
        loaded_cli,
        ConversionOptions(
            target_format=TargetFormat.ANTIGRAVITY_DESKTOP,
            session_id=desktop_id,
            cwd=tmp_path,
        ),
    )
    assert artifact.target_format == TargetFormat.ANTIGRAVITY_DESKTOP
    desktop_home = tmp_path / "dest_desktop"
    native_path, manifest_path = install_antigravity_desktop_artifact(
        artifact,
        target_home=desktop_home,
    )
    assert native_path.is_file()
    assert manifest_path.is_file()

    # 3. Load Desktop -> Convert back to CLI
    loaded_desktop = load_session(native_path)
    assert loaded_desktop.source_format == AgentFormat.ANTIGRAVITY_DESKTOP
    assert loaded_desktop.session_id == desktop_id

    cli_dest_id = "66666666-6666-4666-8666-666666666666"
    cli_artifact = convert_session(
        loaded_desktop,
        ConversionOptions(
            target_format=TargetFormat.ANTIGRAVITY,
            session_id=cli_dest_id,
            cwd=tmp_path,
        ),
    )
    assert cli_artifact.target_format == TargetFormat.ANTIGRAVITY
    assert cli_artifact.native_record_count == artifact.native_record_count
