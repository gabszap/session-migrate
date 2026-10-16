"""Hermes Agent 0.22.2 SQLite session adapter.

Hermes keeps every CLI conversation in ``$HERMES_HOME/state.db`` (normally
``~/.hermes``; on Windows ``%LOCALAPPDATA%\\hermes``).  A single database
contains many sessions, so source selection is always the pair ``(state.db,
native session id)``.  Target bytes use Hermes's own exported-session shape
inside a small versioned envelope; installation delegates the final
transaction to the pinned Hermes ``SessionDB.import_sessions`` implementation,
which is still the native portability boundary in 0.22.2.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from session_migrate.errors import JsonlError, SessionMigrateError
from session_migrate.formats.common import content_text, portable_data_image, string, valid_rfc3339
from session_migrate.jsonl import DEFAULT_MAX_RECORDS, DEFAULT_MAX_TOTAL_BYTES
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

PINNED_HERMES_VERSION = "0.22.2"
PINNED_HERMES_RELEASE_TAG = "v2026.9.24"
PINNED_HERMES_SOURCE_COMMIT = "d923a5eb487a17c0a68801bba9ef0f56c81040a8"
PINNED_HERMES_SCHEMA_VERSION = 31
HERMES_BUNDLE_SCHEMA = "session-migrate.hermes.v1"
HERMES_STATE_FILENAME = "state.db"
MAX_DATABASE_BYTES = 512 * 1024 * 1024
MAX_BUNDLE_BYTES = DEFAULT_MAX_TOTAL_BYTES
MAX_MESSAGES = DEFAULT_MAX_RECORDS
MAX_TEXT_BYTES = 64 * 1024 * 1024
MAX_JSON_DEPTH = 96
MAX_JSON_NODES = 1_000_000

_NATIVE_ID = re.compile(r"(?P<date>\d{8})_(?P<time>\d{6})_(?P<suffix>[0-9a-f]{6,8})\Z")
# 0.22.2 prints build provenance after the release ("· stable · local <sha>"), so the
# version line is matched on its pinned prefix and every suffix is tolerated.
_VERSION_LINE = re.compile(r"^Hermes Agent v(?P<version>[^\s(]+) \((?P<release>[^)]+)\)")
_INSTALL_DIRECTORY_LINE = "install directory"
_REQUIRED_SESSION_COLUMNS = frozenset(
    {
        "id",
        "source",
        "model",
        "model_config",
        "parent_session_id",
        "started_at",
        "ended_at",
        "end_reason",
        "message_count",
        "tool_call_count",
        "cwd",
        "billing_provider",
        "title",
        "title_source",
        "last_activity_at",
        "archived",
        "hidden",
    }
)
_REQUIRED_MESSAGE_COLUMNS = frozenset(
    {
        "id",
        "session_id",
        "role",
        "content",
        "tool_call_id",
        "tool_calls",
        "tool_name",
        "timestamp",
        "finish_reason",
        "reasoning",
        "reasoning_content",
        "reasoning_details",
        "observed",
        "_compressed_summary",
        "active",
        "compacted",
        "api_content",
        "display_kind",
        "display_metadata",
    }
)
_SESSION_EXPORT_FIELDS = frozenset(
    {
        "id",
        "source",
        "user_id",
        "model",
        "model_config",
        "system_prompt",
        "parent_session_id",
        "started_at",
        "ended_at",
        "end_reason",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "cwd",
        "git_branch",
        "git_repo_root",
        "billing_provider",
        "billing_base_url",
        "billing_mode",
        "estimated_cost_usd",
        "actual_cost_usd",
        "cost_status",
        "cost_source",
        "pricing_version",
        "title",
        "api_call_count",
        "archived",
        "messages",
    }
)
_MESSAGE_EXPORT_FIELDS = frozenset(
    {
        "role",
        "content",
        "tool_call_id",
        "tool_calls",
        "tool_name",
        "effect_disposition",
        "timestamp",
        "token_count",
        "finish_reason",
        "reasoning",
        "reasoning_content",
        "reasoning_details",
        "codex_reasoning_items",
        "codex_message_items",
        "platform_message_id",
        "observed",
        "_compressed_summary",
        "api_content",
        "display_kind",
        "display_metadata",
    }
)


@dataclass(frozen=True, slots=True)
class HermesSessionInventory:
    """Content-free metadata for one row in a Hermes state database."""

    session_id: str
    title: str | None
    cwd: Path | None
    started_at: str | None
    cli_version: str | None
    updated_ns: int | None
    records: int | None


@dataclass(frozen=True, slots=True)
class HermesDatabaseSnapshot:
    """A transactionally consistent, content-addressed SQLite snapshot."""

    source_path: Path
    device: int
    inode: int
    size: int
    modified_ns: int
    sha256: str
    data: bytes


@dataclass(frozen=True, slots=True)
class ParsedHermesBundle:
    session_id: str
    title: str | None
    cwd: Path
    started_at: str
    model: str | None
    messages: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class InstalledHermesSession:
    path: Path
    session_id: str


def normalized_session_id(value: str) -> str:
    """Validate and normalize Hermes's timestamp-prefixed native ID."""

    if not isinstance(value, str):
        raise SessionMigrateError("Hermes session ID must be a string")
    normalized = value.strip().lower()
    match = _NATIVE_ID.fullmatch(normalized)
    if not match:
        raise SessionMigrateError(
            "Hermes session ID must look like YYYYMMDD_HHMMSS_ followed by 6-8 hex digits"
        )
    try:
        datetime.strptime(match.group("date") + match.group("time"), "%Y%m%d%H%M%S")
    except ValueError as exc:
        raise SessionMigrateError("Hermes session ID contains an invalid timestamp") from exc
    return normalized


def native_session_id(portable_id: str, timestamp: str) -> str:
    """Map a portable UUID-like ID to a deterministic Hermes native ID."""

    started = valid_rfc3339(timestamp)
    if not started:
        raise SessionMigrateError("Hermes target timestamp must be RFC 3339")
    try:
        suffix = re.sub(r"[^0-9a-f]", "", portable_id.lower())
    except AttributeError as exc:
        raise SessionMigrateError("Hermes portable session ID must be a string") from exc
    if len(suffix) < 6:
        raise SessionMigrateError("Hermes portable session ID has insufficient hexadecimal entropy")
    moment = datetime.fromisoformat(started.replace("Z", "+00:00")).astimezone(UTC)
    return normalized_session_id(moment.strftime("%Y%m%d_%H%M%S_") + suffix[:6])


def hermes_home(
    home: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> Path:
    """Resolve Hermes's native home without creating it.

    Mirrors the CLI's own platform default: ``%LOCALAPPDATA%\\hermes`` on
    Windows (``~/AppData/Local/hermes`` when that variable is unset) and
    ``~/.hermes`` elsewhere.  ``HERMES_HOME`` wins when it is set.  Resolving a
    bare ``~/.hermes`` on Windows named a directory the CLI never writes to, so
    an install landed outside the real store unless the caller exported
    ``HERMES_HOME`` by hand.
    """

    values = os.environ if environ is None else environ
    configured = str(values.get("HERMES_HOME", "")).strip()
    if configured:
        return Path(configured).expanduser()
    user_home = home or Path.home()
    if (sys.platform if platform is None else platform) == "win32":
        local_app_data = str(values.get("LOCALAPPDATA", "")).strip()
        base = Path(local_app_data).expanduser() if local_app_data else user_home / "AppData" / "Local"
        return base / "hermes"
    return user_home / ".hermes"


def state_database_path(
    root: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> Path:
    """Resolve ``state.db`` from an explicit Hermes root or the native default."""

    selected = (
        Path(root).expanduser()
        if root is not None
        else hermes_home(environ=environ, platform=platform)
    )
    return selected / HERMES_STATE_FILENAME


def serialize(
    session: Session,
    *,
    session_id: str,
    cwd: Path,
    cli_version: str = PINNED_HERMES_VERSION,
    model: str | None = None,
    timestamp: str | None = None,
    title: str | None = None,
) -> tuple[bytes, dict[str, int]]:
    """Serialize portable history into Hermes's official session-import shape."""

    if cli_version != PINNED_HERMES_VERSION:
        raise SessionMigrateError(
            f"Hermes target version must be pinned to {PINNED_HERMES_VERSION}"
        )
    native_id = normalized_session_id(session_id)
    started = valid_rfc3339(timestamp) or valid_rfc3339(session.started_at) or _utc_now()
    started_epoch = _epoch(started)
    logical_time = started_epoch
    messages: list[dict[str, Any]] = []
    dropped: Counter[str] = Counter()
    seen_calls: set[str] = set()
    seen_results: set[str] = set()
    tool_names: dict[str, str] = {}

    def append(event: Event, **values: Any) -> None:
        nonlocal logical_time
        candidate = _epoch(event.timestamp) if valid_rfc3339(event.timestamp) else logical_time
        logical_time = max(candidate, logical_time + 0.000001)
        messages.append(_message_defaults(logical_time, **values))

    for event in session.events:
        if event.kind == EventKind.MESSAGE and event.role in {Role.USER, Role.ASSISTANT}:
            if event.text:
                append(event, role=event.role.value, content=event.text)
            if event.payload.get("ui_only_projection") is True:
                dropped["message:ui_only_projection"] += 1
            continue

        if event.kind == EventKind.CONTEXT and event.role == Role.USER:
            image = portable_data_image(event.payload.get("image_url"))
            if event.payload.get("block_type") != "image" or image is None:
                dropped["context:image"] += 1
                continue
            media_type, encoded = image
            append(
                event,
                role="user",
                content=[
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{media_type};base64,{encoded}"},
                    }
                ],
            )
            continue

        if event.kind == EventKind.TOOL_CALL:
            call_id = event.tool_call_id or ""
            if not call_id:
                dropped["tool_call:missing_id"] += 1
                continue
            if call_id in seen_calls:
                dropped["tool_call:duplicate_id"] += 1
                continue
            name = event.tool_name or "unknown_tool"
            if not event.tool_name:
                dropped["tool_call:missing_name"] += 1
            arguments = event.payload.get("input", {})
            if not isinstance(arguments, dict):
                arguments = {"input": arguments}
                dropped["tool_call:non_object_input"] += 1
            if event.payload.get("namespace"):
                dropped["tool_call:namespace"] += 1
            seen_calls.add(call_id)
            tool_names[call_id] = name
            append(
                event,
                role="assistant",
                content=None,
                finish_reason="tool_calls",
                tool_calls=[
                    {
                        "id": call_id,
                        "call_id": call_id,
                        "response_item_id": f"fc_{call_id}",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(
                                arguments,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                    }
                ],
            )
            continue

        if event.kind == EventKind.TOOL_RESULT:
            call_id = event.tool_call_id or ""
            if not call_id:
                dropped["tool_result:missing_id"] += 1
                continue
            if call_id not in seen_calls:
                dropped["tool_result:orphan_id"] += 1
                continue
            if call_id in seen_results:
                dropped["tool_result:duplicate_id"] += 1
                continue
            seen_results.add(call_id)
            if event.tool_name and event.tool_name != tool_names.get(call_id):
                dropped["tool_result:name_mismatch"] += 1
            text = event.text or content_text(event.payload.get("content_blocks")) or ""
            content = _serialized_tool_output(
                text,
                is_error=event.payload.get("is_error") is True,
            )
            append(
                event,
                role="tool",
                content=content,
                tool_call_id=call_id,
                tool_name=tool_names.get(call_id) or event.tool_name or "unknown_tool",
            )
            blocks = event.payload.get("content_blocks")
            if isinstance(blocks, list):
                dropped["tool_result:non_text_content"] += sum(
                    1
                    for block in blocks
                    if not isinstance(block, dict) or block.get("type") != "text"
                )
            continue

        if event.kind == EventKind.COMPACTION and event.text:
            append(
                event,
                role="user",
                content=f"[CONTEXT SUMMARY]:\n{event.text}",
                _compressed_summary=True,
            )
            if event.payload.get("has_boundary_metadata") is True:
                dropped["compaction:boundary_metadata"] += 1
            if event.payload.get("replacement_history_expanded") is True:
                dropped["compaction:replacement_history_expanded"] += 1
            continue

        if event.kind == EventKind.THINKING:
            dropped["thinking:private"] += 1
            if event.payload.get("encrypted_content") or event.payload.get("signature"):
                dropped["thinking:provider_payload"] += 1
            continue

        dropped[_omission_key(event)] += 1

    if not any(message["role"] == "user" for message in messages):
        raise SessionMigrateError("Hermes target requires at least one portable user message")
    bundle_session = {
        "id": native_id,
        "source": "session-migrate",
        "user_id": None,
        "model": model or session.model,
        "model_config": {"_session_migrate_source": session.source_format.value},
        "system_prompt": None,
        "parent_session_id": None,
        "started_at": started_epoch,
        "ended_at": logical_time,
        "end_reason": "session_migrate_import",
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "cwd": str(cwd.resolve()),
        "git_branch": None,
        "git_repo_root": None,
        "billing_provider": session.model_provider,
        "billing_base_url": None,
        "billing_mode": None,
        "estimated_cost_usd": None,
        "actual_cost_usd": None,
        "cost_status": None,
        "cost_source": None,
        "pricing_version": None,
        "title": title or session.title,
        "api_call_count": 0,
        "archived": 0,
        "messages": messages,
    }
    value = {
        "schema": HERMES_BUNDLE_SCHEMA,
        "cli_version": cli_version,
        "schema_version": PINNED_HERMES_SCHEMA_VERSION,
        "session": bundle_session,
    }
    data = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()
    validate_native_bytes(data, native_id)
    return data, dict(sorted((key, count) for key, count in dropped.items() if count))


def validate_native_bytes(data: bytes, session_id: str) -> ParsedHermesBundle:
    """Strictly validate a generated Hermes import bundle."""

    expected_id = normalized_session_id(session_id)
    if not data or len(data) > MAX_BUNDLE_BYTES:
        raise SessionMigrateError("generated Hermes bundle is empty or exceeds the safety limit")
    try:
        value = json.loads(
            data,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
        _validate_json_shape(value)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise SessionMigrateError("generated Hermes bundle is not valid strict UTF-8 JSON") from exc
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "cli_version",
        "schema_version",
        "session",
    }:
        raise SessionMigrateError("generated Hermes bundle has unexpected top-level fields")
    if value.get("schema") != HERMES_BUNDLE_SCHEMA:
        raise SessionMigrateError("generated Hermes bundle has an unsupported schema")
    if value.get("cli_version") != PINNED_HERMES_VERSION:
        raise SessionMigrateError("generated Hermes bundle CLI version is not pinned")
    if value.get("schema_version") != PINNED_HERMES_SCHEMA_VERSION:
        raise SessionMigrateError("generated Hermes bundle database schema is not pinned")
    native = value.get("session")
    if not isinstance(native, dict) or set(native) != _SESSION_EXPORT_FIELDS:
        raise SessionMigrateError(
            "generated Hermes session fields do not match the import contract"
        )
    if normalized_session_id(native.get("id")) != expected_id:
        raise SessionMigrateError("generated Hermes bundle session linkage is invalid")
    if native.get("source") != "session-migrate":
        raise SessionMigrateError("generated Hermes session source is invalid")
    cwd = _safe_text(native.get("cwd"), "Hermes cwd", required=True)
    title = _safe_text(native.get("title"), "Hermes title")
    model = _safe_text(native.get("model"), "Hermes model")
    started_epoch = _finite_timestamp(native.get("started_at"), "Hermes started_at")
    ended_epoch = _finite_timestamp(native.get("ended_at"), "Hermes ended_at")
    if ended_epoch < started_epoch:
        raise SessionMigrateError("generated Hermes session ends before it starts")
    config = native.get("model_config")
    if not isinstance(config, dict) or set(config) != {"_session_migrate_source"}:
        raise SessionMigrateError("generated Hermes model_config is invalid")
    if not isinstance(config["_session_migrate_source"], str):
        raise SessionMigrateError("generated Hermes source marker is invalid")
    if native.get("parent_session_id") is not None or native.get("system_prompt") is not None:
        raise SessionMigrateError("generated Hermes bundle contains private runtime ownership")
    for field in (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "api_call_count",
        "archived",
    ):
        if not isinstance(native.get(field), int) or native[field] < 0:
            raise SessionMigrateError(f"generated Hermes {field} is invalid")
    messages = native.get("messages")
    if not isinstance(messages, list) or not messages or len(messages) > MAX_MESSAGES:
        raise SessionMigrateError("generated Hermes bundle has no messages or too many messages")
    _validate_messages(messages)
    if not any(message["role"] == "user" for message in messages):
        raise SessionMigrateError("generated Hermes bundle contains no user message")
    return ParsedHermesBundle(
        session_id=expected_id,
        title=title,
        cwd=Path(cwd),
        started_at=_timestamp(started_epoch),
        model=model,
        messages=tuple(dict(message) for message in messages),
    )


def native_record_count(data: bytes) -> int:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 0
    native = value.get("session") if isinstance(value, dict) else None
    messages = native.get("messages") if isinstance(native, dict) else None
    return 1 + len(messages) if isinstance(messages, list) else 0


def database_snapshot(path: Path) -> HermesDatabaseSnapshot:
    """Take a bounded SQLite backup that includes committed WAL frames."""

    source = Path(os.path.abspath(path.expanduser()))
    try:
        info = source.lstat()
    except OSError as exc:
        raise JsonlError("Hermes state database is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise JsonlError("Hermes state database must be a regular non-symlink file")
    if info.st_size <= 0 or info.st_size > MAX_DATABASE_BYTES:
        raise JsonlError("Hermes state database violates the input size limit")
    with tempfile.TemporaryDirectory(prefix="session-migrate-hermes-snapshot-") as directory:
        target = Path(directory) / HERMES_STATE_FILENAME
        uri = f"file:{quote(str(source), safe='/')}?mode=ro"
        origin = None
        destination = None
        try:
            origin = sqlite3.connect(uri, uri=True, timeout=5)
            origin.execute("PRAGMA trusted_schema=OFF")
            destination = sqlite3.connect(target)
            origin.backup(destination)
            destination.commit()
            origin.close()
            origin = None
            destination.close()
            destination = None
            data = target.read_bytes()
        except (OSError, sqlite3.Error) as exc:
            raise JsonlError("cannot make a consistent Hermes database snapshot") from exc
        finally:
            if origin is not None:
                origin.close()
            if destination is not None:
                destination.close()
    if not data or len(data) > MAX_DATABASE_BYTES:
        raise JsonlError("Hermes database snapshot violates the input size limit")
    return HermesDatabaseSnapshot(
        source_path=source,
        device=info.st_dev,
        inode=info.st_ino,
        size=len(data),
        modified_ns=info.st_mtime_ns,
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )


def snapshot_database_bytes(path: Path) -> bytes:
    return database_snapshot(path).data


def list_sessions(path: Path) -> tuple[HermesSessionInventory, ...]:
    """Return content-free inventory rows for every session in one store."""

    snapshot = database_snapshot(path)
    with _database_from_bytes(snapshot.data) as db:
        _validate_database(db)
        try:
            rows = db.execute(
                """SELECT id,title,cwd,started_at,message_count,
                          MAX(COALESCE(last_activity_at,0),COALESCE(ended_at,0),started_at)
                             AS updated_at
                   FROM sessions ORDER BY started_at DESC,id DESC"""
            ).fetchall()
        except sqlite3.Error as exc:
            raise JsonlError("cannot inventory Hermes sessions") from exc
    result = []
    for row in rows:
        result.append(
            HermesSessionInventory(
                session_id=normalized_session_id(row["id"]),
                title=string(row["title"]),
                cwd=Path(row["cwd"]) if string(row["cwd"]) else None,
                started_at=_timestamp_or_none(row["started_at"]),
                # Hermes does not persist a per-session CLI version.  Schema
                # validation proves compatibility, but inventing a row value
                # would misrepresent native metadata.
                cli_version=None,
                updated_ns=_nanoseconds_or_none(row["updated_at"]),
                records=int(row["message_count"])
                if isinstance(row["message_count"], int)
                else None,
            )
        )
    return tuple(result)


def parse_session(path: Path, session_id: str | None = None) -> Session:
    """Project one selected Hermes row and its active replay context."""

    snapshot = database_snapshot(path)
    with _database_from_bytes(snapshot.data) as db:
        _validate_database(db)
        selected_id = _select_session_id(db, session_id)
        native = db.execute("SELECT * FROM sessions WHERE id = ?", (selected_id,)).fetchone()
        if native is None:
            raise SessionMigrateError("Hermes session was not found in the selected state.db")
        rows = db.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id ASC", (selected_id,)
        ).fetchall()
    events: list[Event] = []
    for index, row in enumerate(rows):
        if int(row["active"] or 0) != 1:
            events.append(
                Event(
                    EventKind.OPAQUE,
                    Provenance(index, "hermes.message", source_id=str(row["id"])),
                    timestamp=_timestamp_or_none(row["timestamp"]),
                    payload={
                        "reason": "hermes_compacted_history"
                        if int(row["compacted"] or 0) == 1
                        else "hermes_rewound_history"
                    },
                )
            )
            continue
        events.extend(_message_events(row, index))
    provider = string(native["billing_provider"])
    model = string(native["model"])
    if not provider and model and "/" in model:
        provider = model.split("/", 1)[0]
    return Session(
        source_format=AgentFormat.HERMES,
        source_path=Path(path).resolve(),
        source_sha256=snapshot.sha256,
        session_id=selected_id,
        cwd=Path(native["cwd"]) if string(native["cwd"]) else None,
        started_at=_timestamp_or_none(native["started_at"]),
        cli_version=None,
        model=model,
        title=string(native["title"]),
        events=tuple(events),
        raw_record_count=len(rows),
        model_provider=provider,
    )


parse = parse_session


def _cli_report(path: Path, values: Mapping[str, str], *arguments: str) -> str:
    """Run the pinned launcher and return its stdout, refusing a failed invocation."""

    safe_env = _subprocess_environment(values, None)
    try:
        completed = subprocess.run(
            [str(path), *arguments],
            env=safe_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SessionMigrateError("cannot query the Hermes version") from exc
    if completed.returncode != 0:
        raise SessionMigrateError("cannot query the Hermes version")
    return completed.stdout


def _verify_cli(
    executable: Path | None = None, *, environ: Mapping[str, str] | None = None
) -> tuple[Path, str]:
    """Resolve the Hermes launcher, prove it is the pinned build, return its report.

    The full ``--version`` report is kept because 0.22.2 publishes the install
    directory there, which is what locates the Python runtime behind a Windows
    launcher shim (see :func:`_python_invocation`).
    """

    values = dict(os.environ if environ is None else environ)
    candidate = str(executable) if executable else shutil.which("hermes", path=values.get("PATH"))
    if not candidate:
        raise SessionMigrateError("Hermes executable 'hermes' was not found")
    path = Path(candidate).expanduser().resolve()
    try:
        info = path.stat()
    except OSError as exc:
        raise SessionMigrateError("cannot inspect the Hermes executable") from exc
    if not stat.S_ISREG(info.st_mode):
        raise SessionMigrateError("Hermes executable is not a regular file")
    report = _cli_report(path, values, "--version")
    first = report.splitlines()[0].strip() if report.splitlines() else ""
    observed = _VERSION_LINE.match(first)
    expected = f"Hermes Agent v{PINNED_HERMES_VERSION} ({PINNED_HERMES_RELEASE_TAG[1:]})"
    if (
        observed is None
        or observed.group("version") != PINNED_HERMES_VERSION
        or observed.group("release") != PINNED_HERMES_RELEASE_TAG[1:]
    ):
        raise SessionMigrateError(
            f"Hermes version mismatch: expected {expected}, observed {first or '<unavailable>'}"
        )
    return path, report


def verify_pinned_cli(
    executable: Path | None = None, *, environ: Mapping[str, str] | None = None
) -> Path:
    return _verify_cli(executable, environ=environ)[0]


def install_bundle(
    data: bytes,
    *,
    session_id: str,
    target_home: Path,
    target_cli: Path | None = None,
    dry_run: bool = False,
    environ: Mapping[str, str] | None = None,
) -> InstalledHermesSession:
    """Install with Hermes's own ``SessionDB.import_sessions`` transaction."""

    parsed = validate_native_bytes(data, session_id)
    root = _absolute_no_follow(target_home)
    executable, report = _verify_cli(target_cli, environ=environ)
    install_directory = _install_directory(report)
    database = root / HERMES_STATE_FILENAME
    if os.path.lexists(database):
        _assert_no_collision(database, parsed.session_id)
    if dry_run:
        return InstalledHermesSession(database, parsed.session_id)

    _ensure_private_directory(root)
    prefix, prelude = _python_invocation(
        executable, environ=environ, install_directory=install_directory
    )
    environment = _subprocess_environment(environ, root)
    if not database.exists():
        _initialize_state_database(prefix, prelude, database, environment)

    helper = (
        "import json,sys; from pathlib import Path; from hermes_state import SessionDB; "
        "value=json.loads(sys.stdin.buffer.read()); db=SessionDB(db_path=Path(sys.argv[1])); "
        "result=db.import_sessions([value['session']]); db.close(); "
        "print(json.dumps(result,separators=(',',':')))"
    )
    try:
        code, stdout, stderr = _run_helper(
            prefix, prelude, helper, database=database, payload=data, environment=environment
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SessionMigrateError("Hermes session import failed") from exc
    result = _last_json_object(stdout)
    if (
        code != 0
        or result.get("ok") is not True
        or result.get("imported_ids") != [parsed.session_id]
        or result.get("skipped") != 0
    ):
        detail = result.get("errors")
        if not detail and stderr:
            detail = stderr.splitlines()[-1][:500]
        suffix = f": {detail}" if detail else ""
        raise SessionMigrateError(f"Hermes refused the imported session bundle{suffix}")
    loaded = parse_session(database, parsed.session_id)
    if loaded.session_id != parsed.session_id:
        raise SessionMigrateError("installed Hermes session failed identity validation")
    return InstalledHermesSession(database, parsed.session_id)


def _initialize_state_database(
    prefix: list[str], prelude: str, database: Path, environment: Mapping[str, str]
) -> None:
    """Create a missing store through Hermes's own schema initializer.

    ``hermes sessions list`` no longer creates ``state.db`` on 0.22.2 — a read of
    an absent store is simply empty — so the store is opened through the native
    ``SessionDB`` constructor instead, which creates and migrates the file.
    """

    helper = (
        "import sys; from pathlib import Path; from hermes_state import SessionDB; "
        "db=SessionDB(db_path=Path(sys.argv[1])); db.close()"
    )
    try:
        code, _, stderr = _run_helper(
            prefix, prelude, helper, database=database, payload=None, environment=environment
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SessionMigrateError("cannot initialize the Hermes state database") from exc
    if code != 0 or not database.is_file():
        detail = stderr.splitlines()[-1][:500] if stderr else ""
        suffix = f": {detail}" if detail else ""
        raise SessionMigrateError(f"Hermes failed to initialize its state database{suffix}")


def _run_helper(
    prefix: list[str],
    prelude: str,
    source: str,
    *,
    database: Path,
    payload: bytes | None,
    environment: Mapping[str, str],
) -> tuple[int, str, str]:
    """Run one helper inside the Hermes runtime, moving any payload as bytes.

    The bundle crosses the process boundary as UTF-8 bytes and the helper reads
    ``sys.stdin.buffer``.  Passing it as text instead encodes it with the
    *caller's* locale codec while the store's interpreter always decodes UTF-8,
    so a Windows console whose code page is not UTF-8 replaced every non-ASCII
    character with U+FFFD inside the target store.  Both directions are decoded
    explicitly here for the same reason.
    """

    arguments: dict[str, Any] = {}
    if payload is None:
        arguments["stdin"] = subprocess.DEVNULL
    else:
        arguments["input"] = payload
    completed = subprocess.run(
        [*prefix, prelude + source, str(database)],
        env=dict(environment),
        capture_output=True,
        timeout=120,
        check=False,
        **arguments,
    )
    return (
        completed.returncode,
        completed.stdout.decode("utf-8", "replace"),
        completed.stderr.decode("utf-8", "replace"),
    )


def _message_events(row: sqlite3.Row, index: int) -> list[Event]:
    provenance = Provenance(index, "hermes.message", source_id=str(row["id"]))
    timestamp = _timestamp_or_none(row["timestamp"])
    role_value = string(row["role"])
    if int(row["_compressed_summary"] or 0) == 1:
        summary = _decoded_content(row["content"])
        text = _content_text_value(summary)
        if text.startswith("[CONTEXT SUMMARY]:"):
            text = text.removeprefix("[CONTEXT SUMMARY]:").lstrip()
        return [
            Event(
                EventKind.COMPACTION,
                provenance,
                role=Role.SYSTEM,
                text=text,
                timestamp=timestamp,
                payload={"source_subtype": "hermes_compressed_summary"},
            )
        ]
    events: list[Event] = []
    content = _decoded_content(row["content"])
    if role_value in {"user", "assistant"}:
        role = Role.USER if role_value == "user" else Role.ASSISTANT
        text = _content_text_value(content)
        if text:
            events.append(
                Event(
                    EventKind.MESSAGE,
                    provenance,
                    role=role,
                    text=text,
                    timestamp=timestamp,
                )
            )
        if role == Role.USER and isinstance(content, list):
            for block_index, block in enumerate(content):
                image_url = _image_url(block)
                if image_url:
                    events.append(
                        Event(
                            EventKind.CONTEXT,
                            Provenance(
                                index,
                                "hermes.message",
                                source_id=str(row["id"]),
                                block_index=block_index,
                            ),
                            role=Role.USER,
                            timestamp=timestamp,
                            payload={"block_type": "image", "image_url": image_url},
                        )
                    )
        for call_index, tool_call in enumerate(_tool_calls(row["tool_calls"])):
            function = tool_call["function"]
            events.append(
                Event(
                    EventKind.TOOL_CALL,
                    Provenance(
                        index,
                        "hermes.message.tool_call",
                        source_id=str(row["id"]),
                        block_index=call_index,
                    ),
                    role=Role.ASSISTANT,
                    timestamp=timestamp,
                    tool_name=function["name"],
                    tool_call_id=tool_call["id"],
                    payload={"input": _arguments(function["arguments"])},
                )
            )
    elif role_value == "tool":
        text, is_error = _tool_output(content)
        events.append(
            Event(
                EventKind.TOOL_RESULT,
                provenance,
                role=Role.TOOL,
                text=text,
                timestamp=timestamp,
                tool_name=string(row["tool_name"]),
                tool_call_id=string(row["tool_call_id"]),
                payload={
                    "is_error": is_error,
                    "content_blocks": [{"type": "text", "text": text}],
                },
            )
        )
    else:
        events.append(
            Event(
                EventKind.OPAQUE,
                provenance,
                timestamp=timestamp,
                payload={"reason": f"hermes_{role_value or 'unknown'}_message"},
            )
        )
    if row["reasoning"] or row["reasoning_content"] or row["reasoning_details"]:
        events.append(
            Event(
                EventKind.OPAQUE,
                provenance,
                role=Role.ASSISTANT,
                timestamp=timestamp,
                payload={"reason": "hermes_private_reasoning"},
            )
        )
    return events


def _validate_database(db: sqlite3.Connection) -> None:
    try:
        integrity = db.execute("PRAGMA integrity_check(1)").fetchone()
        version = db.execute("SELECT version FROM schema_version").fetchone()
        session_columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
        message_columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
    except sqlite3.Error as exc:
        raise JsonlError("Hermes database schema is unreadable") from exc
    if integrity is None or integrity[0] != "ok":
        raise JsonlError("Hermes database failed SQLite integrity validation")
    if version is None or version[0] != PINNED_HERMES_SCHEMA_VERSION:
        raise JsonlError(
            f"unsupported Hermes database schema; expected {PINNED_HERMES_SCHEMA_VERSION}"
        )
    if not _REQUIRED_SESSION_COLUMNS.issubset(session_columns):
        raise JsonlError("Hermes sessions table does not match the pinned schema")
    if not _REQUIRED_MESSAGE_COLUMNS.issubset(message_columns):
        raise JsonlError("Hermes messages table does not match the pinned schema")


@contextmanager
def _database_from_bytes(data: bytes) -> Iterator[sqlite3.Connection]:
    with tempfile.TemporaryDirectory(prefix="session-migrate-hermes-read-") as directory:
        path = Path(directory) / HERMES_STATE_FILENAME
        path.write_bytes(data)
        uri = f"file:{quote(str(path), safe='/')}?mode=ro&immutable=1"
        try:
            db = sqlite3.connect(uri, uri=True, timeout=5)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA query_only=ON")
        except sqlite3.Error as exc:
            raise JsonlError("cannot open Hermes database snapshot") from exc
        try:
            yield db
        finally:
            db.close()


def _select_session_id(db: sqlite3.Connection, session_id: str | None) -> str:
    if session_id is not None:
        selected = normalized_session_id(session_id)
        if db.execute("SELECT 1 FROM sessions WHERE id = ?", (selected,)).fetchone() is None:
            raise SessionMigrateError("Hermes session was not found in the selected state.db")
        return selected
    rows = db.execute("SELECT id FROM sessions ORDER BY started_at DESC,id DESC LIMIT 2").fetchall()
    if not rows:
        raise SessionMigrateError("Hermes state database contains no sessions")
    if len(rows) != 1:
        raise SessionMigrateError(
            "Hermes state database contains multiple sessions; select one by native ID"
        )
    return normalized_session_id(rows[0]["id"])


def _validate_messages(messages: list[Any]) -> None:
    seen_calls: set[str] = set()
    seen_results: set[str] = set()
    previous_timestamp = -math.inf
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or set(message) != _MESSAGE_EXPORT_FIELDS:
            raise SessionMigrateError(f"generated Hermes message {index} fields are invalid")
        role = message.get("role")
        if role not in {"user", "assistant", "tool"}:
            raise SessionMigrateError(f"generated Hermes message {index} role is invalid")
        timestamp = _finite_timestamp(message.get("timestamp"), "Hermes message timestamp")
        if timestamp < previous_timestamp:
            raise SessionMigrateError("generated Hermes message timestamps are not monotonic")
        previous_timestamp = timestamp
        _validate_content(message.get("content"), role)
        if not isinstance(message.get("observed"), bool):
            raise SessionMigrateError("generated Hermes observed flag is invalid")
        if not isinstance(message.get("_compressed_summary"), bool):
            raise SessionMigrateError("generated Hermes compaction flag is invalid")
        calls = message.get("tool_calls")
        if calls is not None:
            if role != "assistant" or not isinstance(calls, list) or not calls:
                raise SessionMigrateError("generated Hermes tool call container is invalid")
            for call in calls:
                call_id, _, _ = _validate_tool_call(call)
                if call_id in seen_calls:
                    raise SessionMigrateError("generated Hermes tool call ID is duplicated")
                seen_calls.add(call_id)
        if role == "tool":
            call_id = _safe_text(
                message.get("tool_call_id"), "Hermes tool result ID", required=True
            )
            _safe_text(message.get("tool_name"), "Hermes tool result name", required=True)
            if call_id not in seen_calls or call_id in seen_results:
                raise SessionMigrateError("generated Hermes tool result linkage is invalid")
            seen_results.add(call_id)
        elif message.get("tool_call_id") is not None or message.get("tool_name") is not None:
            raise SessionMigrateError(
                "generated Hermes non-tool message carries tool-result fields"
            )
        if message.get("_compressed_summary") and role != "user":
            raise SessionMigrateError("generated Hermes compaction marker must use the user role")


def _validate_tool_call(call: Any) -> tuple[str, str, dict[str, Any]]:
    if not isinstance(call, dict) or set(call) != {
        "id",
        "call_id",
        "response_item_id",
        "type",
        "function",
    }:
        raise SessionMigrateError("generated Hermes tool call fields are invalid")
    call_id = _safe_text(call.get("id"), "Hermes tool call ID", required=True)
    if call.get("call_id") != call_id or call.get("type") != "function":
        raise SessionMigrateError("generated Hermes tool call linkage is invalid")
    _safe_text(call.get("response_item_id"), "Hermes response item ID", required=True)
    function = call.get("function")
    if not isinstance(function, dict) or set(function) != {"name", "arguments"}:
        raise SessionMigrateError("generated Hermes function call is invalid")
    name = _safe_text(function.get("name"), "Hermes function name", required=True)
    arguments = _arguments(function.get("arguments"), strict=True)
    return call_id, name, arguments


def _validate_content(value: Any, role: str) -> None:
    if value is None:
        if role == "tool":
            raise SessionMigrateError("generated Hermes tool result content is missing")
        return
    if isinstance(value, str):
        _safe_text(value, "Hermes message content", required=role in {"user", "tool"})
        return
    if role != "user" or not isinstance(value, list) or not value:
        raise SessionMigrateError("generated Hermes structured content is invalid")
    for block in value:
        if not isinstance(block, dict) or set(block) != {"type", "image_url"}:
            raise SessionMigrateError("generated Hermes image block is invalid")
        image = block.get("image_url")
        if (
            block.get("type") != "image_url"
            or not isinstance(image, dict)
            or set(image) != {"url"}
            or portable_data_image(image.get("url")) is None
        ):
            raise SessionMigrateError("generated Hermes image URL is invalid")


def _message_defaults(timestamp: float, **values: Any) -> dict[str, Any]:
    result = {field: None for field in _MESSAGE_EXPORT_FIELDS}
    result.update(
        {
            "role": values.pop("role"),
            "content": values.pop("content", None),
            "timestamp": timestamp,
            "observed": False,
            "_compressed_summary": False,
        }
    )
    result.update(values)
    return result


def _tool_calls(value: Any) -> list[dict[str, Any]]:
    if not value:
        return []
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    result = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        call_id = string(item.get("id")) or string(item.get("call_id"))
        name = string(function.get("name")) if isinstance(function, dict) else None
        arguments = function.get("arguments") if isinstance(function, dict) else None
        if call_id and name and isinstance(arguments, (str, dict)):
            result.append({"id": call_id, "function": {"name": name, "arguments": arguments}})
    return result


def _decoded_content(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.lstrip()
    if not stripped.startswith("["):
        return value
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    return parsed if isinstance(parsed, list) else value


def _content_text_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    return "\n".join(
        text
        for block in value
        if isinstance(block, dict)
        for text in [string(block.get("text"))]
        if text
    )


def _image_url(block: Any) -> str | None:
    if not isinstance(block, dict) or block.get("type") not in {"image_url", "input_image"}:
        return None
    image = block.get("image_url")
    value = string(image.get("url")) if isinstance(image, dict) else string(image)
    if not value:
        value = string(block.get("image_url")) or string(block.get("url"))
    return value if value and portable_data_image(value) else None


def _tool_output(value: Any) -> tuple[str, bool]:
    text = _content_text_value(value)
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text, False
    if not isinstance(parsed, dict):
        return text, False
    output = parsed.get("output")
    error = parsed.get("error")
    exit_code = parsed.get("exit_code")
    return (output if isinstance(output, str) else text), bool(error) or (
        isinstance(exit_code, int) and exit_code != 0
    )


def _serialized_tool_output(text: str, *, is_error: bool) -> str:
    """Encode an envelope when Hermes cannot store the result as plain text."""

    if text and "\x00" not in text and not is_error:
        return text
    return json.dumps(
        {
            "output": text,
            "error": (text or True) if is_error else None,
            "exit_code": 1 if is_error else 0,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _arguments(value: Any, *, strict: bool = False) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            if strict:
                raise SessionMigrateError("generated Hermes tool arguments are invalid") from exc
            return {"input": value}
        if isinstance(parsed, dict):
            return parsed
    if strict:
        raise SessionMigrateError("generated Hermes tool arguments must be a JSON object")
    return {"input": value}


def _assert_no_collision(database: Path, session_id: str) -> None:
    snapshot = database_snapshot(database)
    with _database_from_bytes(snapshot.data) as db:
        _validate_database(db)
        if db.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone():
            raise SessionMigrateError("Hermes session ID already exists; refusing to overwrite it")


def title_taken(database: Path, title: str | None) -> bool:
    """Report whether *title* is already held by a session in this store.

    Hermes keeps ``idx_sessions_title_unique`` (a partial unique index over
    non-null titles), so a second session cannot reuse one.  The title is
    cosmetic — Hermes's own importer discards it on conflict — so a caller can
    install without it rather than lose the whole migration.

    The lookup is advisory: the import transaction still enforces the real
    constraint.  A missing, unreadable, or concurrently written store reports
    ``False`` and leaves the decision to that transaction.
    """

    if not title:
        return False
    if not os.path.lexists(database):
        return False
    uri = f"file:{quote(str(database), safe='/')}?mode=ro"
    connection = None
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.execute("PRAGMA trusted_schema=OFF")
        row = connection.execute("SELECT 1 FROM sessions WHERE title = ?", (title,)).fetchone()
    except (OSError, sqlite3.Error):
        return False
    finally:
        if connection is not None:
            connection.close()
    return row is not None


def without_title(data: bytes) -> bytes:
    """Return Hermes bundle bytes whose session carries no title.

    Only the title changes: every other field of the validated envelope is
    re-emitted byte-identically in meaning, and the result is re-validated by
    the installer before it is used.
    """

    value = _bundle_object(data)
    value["session"]["title"] = None
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode()


def bundle_title(data: bytes) -> str | None:
    """Title carried by a Hermes bundle, when it has one."""

    return _safe_text(_bundle_object(data)["session"].get("title"), "Hermes title")


def _bundle_object(data: bytes) -> dict[str, Any]:
    """Strictly parse a bundle envelope without re-running full validation."""

    try:
        value = json.loads(
            data,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise SessionMigrateError("generated Hermes bundle is not valid strict UTF-8 JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("session"), dict):
        raise SessionMigrateError("generated Hermes bundle has no session to adjust")
    return value


def _script_interpreter(executable: Path) -> Path:
    try:
        with executable.open("rb") as stream:
            first = stream.readline(4096)
    except OSError as exc:
        raise SessionMigrateError("cannot inspect the Hermes launcher") from exc
    if not first.startswith(b"#!"):
        raise SessionMigrateError("Hermes launcher does not expose its Python interpreter")
    try:
        words = first[2:].decode("utf-8").strip().split()
    except UnicodeDecodeError as exc:
        raise SessionMigrateError("Hermes launcher shebang is not UTF-8") from exc
    if len(words) != 1 or not words[0].startswith("/"):
        raise SessionMigrateError("Hermes launcher shebang is not a pinned absolute interpreter")
    # Keep the shebang path itself.  A virtualenv's ``python`` is normally a
    # symlink; resolving it to the base interpreter discards ``pyvenv.cfg``
    # discovery and therefore the installed ``hermes_state`` package.
    interpreter = Path(words[0])
    try:
        info = interpreter.stat()
    except OSError as exc:
        raise SessionMigrateError("Hermes Python interpreter is unavailable") from exc
    if not stat.S_ISREG(info.st_mode):
        raise SessionMigrateError("Hermes Python interpreter is not a regular file")
    return interpreter


def _install_directory(report: str) -> Path | None:
    """Read the ``Install directory:`` line from a ``hermes --version`` report.

    Absent on builds that do not print it; the caller then falls back to the
    launcher-derived interpreter.
    """

    for line in report.splitlines():
        label, separator, value = line.partition(":")
        if separator and label.strip().lower() == _INSTALL_DIRECTORY_LINE and value.strip():
            return Path(value.strip())
    return None


def _runtime_command(
    executable: Path, values: Mapping[str, str]
) -> list[str] | None:
    """Ask the launcher for the exact command that runs Hermes in this install.

    ``hermes --print-runtime-command`` prints a JSON argv whose first element is
    the interpreter.  It is the only portable way to reach the runtime behind a
    compiled Windows launcher, whose file head is not a shebang.  Older builds
    do not implement the flag, so a failure returns ``None`` instead of raising.
    """

    safe_env = _subprocess_environment(values, None)
    try:
        completed = subprocess.run(
            [str(executable), "--print-runtime-command"],
            env=safe_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    for line in reversed(completed.stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
            return value
    return None


def _bootstrap_root(runtime: list[str]) -> Path | None:
    """Recover the install root from the launcher's own bootstrap source.

    Used when ``hermes --version`` does not publish an install directory: the
    runtime command the launcher prints inserts its root into ``sys.path``, so
    the helper can reuse the very same path the CLI itself imports from.
    """

    pattern = re.compile(r"sys\.path\.insert\(\s*0\s*,\s*['\"](?P<root>[^'\"]+)['\"]\s*\)")
    for argument in runtime:
        match = pattern.search(argument)
        if match:
            return Path(match.group("root").replace("\\\\", "\\"))
    return None


def _python_invocation(
    executable: Path, *, environ: Mapping[str, str] | None, install_directory: Path | None
) -> tuple[list[str], str]:
    """Build the argv prefix and prelude that run a helper inside pinned Hermes.

    The command is completed by ``[*prefix, prelude + source, *arguments]``.  The
    prelude reproduces what the published launcher does for itself before the
    entry point runs: it clears ``PYTHONHOME``/``PYTHONPATH``/``VIRTUAL_ENV``,
    puts the install root on ``sys.path`` and then imports ``hermes_bootstrap``,
    which selects and activates the store's dependency generation.  Store Python
    alone has no third-party packages, so skipping the bootstrap import makes
    ``hermes_state`` — and everything it imports — unresolvable.

    ``hermes --print-runtime-command`` is the documented machine boundary for
    consumers holding the launcher, and it is the only source on a Windows
    install, whose launcher is a compiled zipapp rather than a ``#!`` script.
    Builds predating the flag fall back to the launcher's own shebang.
    """

    values = dict(os.environ if environ is None else environ)
    runtime = _runtime_command(executable, values)
    if runtime is not None:
        interpreter = Path(runtime[0]).expanduser()
        try:
            info = interpreter.stat()
        except OSError as exc:
            raise SessionMigrateError("Hermes Python interpreter is unavailable") from exc
        if not stat.S_ISREG(info.st_mode):
            raise SessionMigrateError("Hermes Python interpreter is not a regular file")
        root = install_directory or _bootstrap_root(runtime)
        prelude = [
            "import os,sys",
            "os.environ.pop('PYTHONHOME',None)",
            "os.environ.pop('PYTHONPATH',None)",
            "os.environ.pop('VIRTUAL_ENV',None)",
        ]
        if root is not None:
            prelude.append(f"sys.path.insert(0,{str(root)!r})")
        prelude.append("import hermes_bootstrap")
        return [str(interpreter), "-I", "-X", "utf8", "-c"], "; ".join(prelude) + "; "
    return [str(_script_interpreter(executable)), "-X", "utf8", "-c"], ""


def _subprocess_environment(
    environ: Mapping[str, str] | None, target_home: Path | None
) -> dict[str, str]:
    values = dict(os.environ if environ is None else environ)
    result = {
        key: value
        for key, value in values.items()
        if key
        in {
            "HOME",
            "PATH",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TMPDIR",
            "TERM",
            # Windows: the CLI resolves its own home through USERPROFILE (or
            # HOMEDRIVE/HOMEPATH) and refuses to start without it, and a child
            # process inherits its loader/search paths from SYSTEMROOT,
            # PATHEXT and COMSPEC.  Dropping them fails the launcher before
            # ``--version`` can print anything.
            "USERPROFILE",
            "HOMEDRIVE",
            "HOMEPATH",
            "APPDATA",
            "LOCALAPPDATA",
            "PROGRAMDATA",
            "SYSTEMROOT",
            "SYSTEMDRIVE",
            "WINDIR",
            "PATHEXT",
            "COMSPEC",
            "TEMP",
            "TMP",
            "NUMBER_OF_PROCESSORS",
        }
    }
    result.setdefault("LC_ALL", "C")
    result.setdefault("TERM", "dumb")
    result["NO_COLOR"] = "1"
    result["HERMES_NO_UPDATE_CHECK"] = "1"
    # A caller that redirects the home through HOME (the pinned native tests)
    # must reach the same home on Windows, where Path.home() reads USERPROFILE.
    if result.get("HOME") and not result.get("USERPROFILE"):
        result["USERPROFILE"] = result["HOME"]
    if target_home is not None:
        result["HERMES_HOME"] = str(target_home)
    return result


def _last_json_object(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _absolute_no_follow(path: Path) -> Path:
    candidate = Path(os.path.abspath(path.expanduser()))
    cursor = candidate
    while True:
        if os.path.lexists(cursor) and stat.S_ISLNK(cursor.lstat().st_mode):
            raise SessionMigrateError("Hermes target path must not traverse symlinks")
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    return candidate


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        info = path.lstat()
    except OSError as exc:
        raise SessionMigrateError("cannot inspect the Hermes target home") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SessionMigrateError("Hermes target home is not a regular directory")
    path.chmod(0o700)


def _safe_text(value: Any, label: str, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value) or "\x00" in (value or ""):
        raise SessionMigrateError(f"generated {label} is invalid")
    if len(value.encode()) > MAX_TEXT_BYTES:
        raise SessionMigrateError(f"generated {label} exceeds the text size limit")
    return value


def _finite_timestamp(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SessionMigrateError(f"generated {label} is invalid")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise SessionMigrateError(f"generated {label} is invalid")
    return result


def _epoch(value: str | None) -> float:
    valid = valid_rfc3339(value)
    if not valid:
        return datetime.now(UTC).timestamp()
    return datetime.fromisoformat(valid.replace("Z", "+00:00")).timestamp()


def _timestamp(value: float) -> str:
    return datetime.fromtimestamp(float(value), UTC).isoformat().replace("+00:00", "Z")


def _timestamp_or_none(value: Any) -> str | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    try:
        return _timestamp(number)
    except (OverflowError, OSError, ValueError):
        return None


def _nanoseconds_or_none(value: Any) -> int | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number * 1_000_000_000) if math.isfinite(number) and number >= 0 else None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _omission_key(event: Event) -> str:
    role = event.role.value if event.role else "none"
    return f"{event.kind.value}:{role}"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value}")


def _validate_json_shape(value: Any) -> None:
    remaining = MAX_JSON_NODES
    stack = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        remaining -= 1
        if remaining < 0:
            raise ValueError("JSON node limit exceeded")
        if depth > MAX_JSON_DEPTH:
            raise ValueError("JSON depth limit exceeded")
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
