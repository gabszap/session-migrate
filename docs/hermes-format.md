# Hermes Agent format

This document pins the native Hermes Agent contract implemented by
`session-migrate`. Hermes's SQLite layout is an internal, versioned interface,
so the adapter fails closed on any other Hermes or schema version.

## Compatibility pin

| Item | Validated value |
| --- | --- |
| Project | [`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent) |
| Release tag | `v2026.9.24` (also published as `v0.22.2`) |
| Python package | `hermes-agent==0.22.2` |
| Source commit | `d923a5eb487a17c0a68801bba9ef0f56c81040a8` |
| Native SQLite schema | `31` |
| Observed CLI version | `Hermes Agent v0.22.2 (2026.9.24) · stable · local d923a5eb487a` |

The adapter now pins the same release the published launcher reports, instead of
carrying a source-archive hash: a 0.22.2 git install reports its own build
provenance on the version line, so the release identity is read from the
launcher itself (`Hermes Agent v<version> (<release>)`, with any trailing
`· stable · local <sha>` suffix tolerated) rather than from an artifact hash
that a rebuild would invalidate.

## Native store and discovery

Hermes stores all CLI sessions in one SQLite database:

```text
$HERMES_HOME/state.db
```

`HERMES_HOME` defaults to `~/.hermes`, or to `%LOCALAPPDATA%\hermes` on Windows.
Unlike file-per-session harnesses, a
Hermes catalog entry is virtual: its identity is the pair `state.db` plus the
native session ID. Native IDs have the form:

```text
YYYYMMDD_HHMMSS_abcdef
```

The adapter validates the timestamp and the 6-to-8-character hexadecimal
suffix. `list_sessions()` inventories every row without loading message bodies.
It returns `session_id`, `title`, `cwd`, `started_at`, `updated_ns`, and the
stored message count. `cli_version` is `None` because Hermes does not persist a
per-session CLI version; database schema validation supplies the compatibility
gate instead.

The schema-31 tables used by the adapter are:

| Table | Fields used |
| --- | --- |
| `schema_version` | `version` |
| `sessions` | identity, title, CWD, model/provider, start/end/activity time, message count |
| `messages` | role/content, tool linkage, timestamp, private reasoning markers, active/compacted state |

Schema 31 keeps every column the adapter requires from schema 26, so the
projection and the required-column gates are unchanged; only the pinned version
value moved.

Discovery and parsing first take a bounded SQLite backup. This includes
committed WAL frames and gives the catalog a stable SHA-256 fingerprint. A
symlink, non-regular file, oversized database, integrity failure, missing
columns, or schema other than 31 is rejected.

## Source projection

`parse_session(state_db, session_id=...)` requires an explicit native ID when
the database contains more than one session. It reads messages in database
order and maps only Hermes's active replay context:

| Hermes record | Portable event |
| --- | --- |
| `user` or `assistant` text | message |
| user `image_url` data block | image context |
| assistant `tool_calls` | linked tool call |
| `tool` row | linked tool result |
| `_compressed_summary=1` | compaction summary |
| inactive, `compacted=1` row | opaque `hermes_compacted_history` marker |
| other inactive row | opaque `hermes_rewound_history` marker |
| persisted reasoning fields | opaque `hermes_private_reasoning` marker |

Private reasoning text is never copied. Inactive rows are retained only as one
reason-specific accounting marker per row; replaying them would reintroduce
history that Hermes itself no longer sends to the model.

## Target bundle and installation

The serializer creates strict UTF-8 JSON with envelope schema
`session-migrate.hermes.v1`. Its `session` object is the exact shape returned by
Hermes's native session exporter. User and assistant text, inline data images,
linked function calls/results, title, CWD, model metadata, and compaction
summaries are represented natively. Timestamps are made monotonic without
changing semantic order.

Installation uses Hermes's own `SessionDB.import_sessions` transaction from the
pinned environment. It does not issue ad hoc SQL writes. The public adapter
entry point is:

```python
install_bundle(
    data: bytes,
    *,
    session_id: str,
    target_home: Path,
    target_cli: Path | None = None,
    dry_run: bool = False,
    environ: Mapping[str, str] | None = None,
) -> InstalledHermesSession
```

The installer verifies the exact CLI version, creates a missing `state.db`
through the native `SessionDB` initializer, refuses an existing session ID,
invokes the native importer, and reparses the installed row before returning.
Target directories are private and symlink traversal is rejected.

Hermes's user-facing `hermes sessions import` command currently accepts Claude
and Codex sources, not Hermes's own export. `SessionDB.import_sessions` remains
the native export/import transaction in 0.22.2 and is therefore still the pinned
installation boundary here.

## The native runtime behind the launcher

0.22.2 publishes its own launch command, and the adapter uses it instead of
guessing a Python from the launcher's file head:

```bash
hermes --print-runtime-command   # JSON argv: [interpreter, -I, -c, bootstrap, ...]
hermes --version                 # also prints "Install directory: <root>"
```

That interpreter is *store Python*: it deliberately ships with no third-party
packages. The published launcher reaches the dependency generation by importing
`hermes_bootstrap` after putting the install root on `sys.path`, so the helper
the installer runs reproduces exactly that prelude (clear `PYTHONHOME`,
`PYTHONPATH` and `VIRTUAL_ENV`; insert the install root; import
`hermes_bootstrap`). Without it, every `hermes_state` import fails on a missing
`ruamel`, not on anything session-related. Builds predating
`--print-runtime-command` keep the older behaviour: the adapter derives the
interpreter from the launcher's `#!` line.

Two platform facts are handled explicitly:

- **Windows launchers are compiled zipapps.** `hermes.exe` is a PE stub with the
  launcher's `__main__.py` appended, so there is no `#!` line the old code could
  read; `--print-runtime-command` is the only portable source. The install
  directory is recovered from `Install directory:`, or from the launcher's own
  `sys.path.insert(0, ...)` bootstrap when that line is absent.
- **A sanitized child environment must keep the Windows essentials.** The CLI
  refuses to start when it cannot resolve a home directory, and a Windows child
  needs `SYSTEMROOT`, `PATHEXT` and `COMSPEC` to load at all. The allowlist
  therefore carries `USERPROFILE`, `HOMEDRIVE`, `HOMEPATH`, `APPDATA`,
  `LOCALAPPDATA`, `PROGRAMDATA`, `SYSTEMROOT`, `SYSTEMDRIVE`, `WINDIR`,
  `PATHEXT`, `COMSPEC`, `TEMP`, `TMP` and `NUMBER_OF_PROCESSORS` alongside the
  POSIX variables; `HOME` is mirrored into `USERPROFILE` when the caller
  redirects the home through `HOME` only.

Installation summary: `hermes sessions list` no longer creates `state.db` on a
read of an absent store, so initialization moved to the native `SessionDB`
constructor; and the identity-guard descriptor that reserves the manifest is
closed before a rollback unlinks it, because Windows refuses to delete a file
that still has an open handle (`WinError 32`).

Resume uses Hermes's native command:

```bash
hermes chat --resume YYYYMMDD_HHMMSS_abcdef
```

Hermes also exposes `--continue <title>`, but migration and verification use the
unambiguous session ID.

## What is omitted

| Source data | Behavior | Reason |
| --- | --- | --- |
| Private thinking text/signatures | counted, not serialized | private provider state is not portable |
| System prompt and provider credentials | not serialized | runtime ownership and secrets stay with the target |
| Tool namespaces | tool name/arguments kept; namespace counted | no equivalent portable Hermes field |
| Non-text tool-result blocks | text kept; other blocks counted | native tool row is text-oriented |
| Compaction boundary/replacement metadata | summary kept; metadata counted | Hermes stores the active summary, not portable boundary semantics |
| Rewound or already compacted bodies | opaque accounting only | not part of Hermes's active model replay |
| Token/cost/runtime counters | reset | derived native runtime state |

## Native research trajectories

Research and validation ran in a unique mode-`0700` temporary root. The pinned
source was installed into its own virtual environment; no user Hermes home or
credential store was read or copied.

The 0.20.6 trajectories below remain the historical record of the original
adapter research. The 0.22.2 promotion was re-validated on a live Windows
install:

1. Read side, against the real store, read-only: a 31 MB `state.db` at schema
   `31` was snapshotted (bounded SQLite backup), all required session and
   message columns were present, and inventory returned every session row.
2. Write side, in an isolated scratch home: a generated bundle installed through
   `SessionDB.import_sessions`, was reparsed (title, CWD, both message events),
   appeared in `list_sessions` with the right record count, and a second install
   of the same native ID was refused with the session store byte-identical
   afterwards.
3. End-to-end route: a real Claude Code session (23 records) transferred with
   `smigrate transfer --from claude --to hermes`. The native CLI listed the
   result — `message_count` 22, `tool_call_count` 9, nine linked `tool` rows
   carrying `tool_call_id` and `tool_name`, the source title and CWD preserved,
   `source=session-migrate` — which is the fidelity the text-only native
   importer does not provide: `hermes sessions import` writes `[ran tool: Bash]`
   markers into the assistant body and leaves `tool_call_count=0`.

Not verified in this round: a live `hermes chat --resume` against a provider,
native compaction after import, and WAL-snapshot behaviour while another writer
is appending. Those remain covered by the opt-in native test rather than by
this promotion.

Three native trajectories were inspected:

1. A real Hermes CLI session containing a user turn, assistant function call,
   terminal result, and final assistant response. Native ID
   `20260830_233734_3ff33e`; the tool linkage persisted as
   `call_hermes_1` and the result body as Hermes's terminal-result JSON.
2. A real `hermes sessions import` of a Claude fixture. Native ID
   `20260830_233912_1445cf`; Hermes recorded `source=claude-code` and CWD
   `/work`. This established what the public importer supports and what it does
   not.
3. A generated Hermes bundle imported through `SessionDB.import_sessions`,
   resumed by the real CLI against a loopback OpenAI-compatible provider, and
   compacted through the native `SessionDB.archive_and_compact` API. The same
   isolated store was also given a malformed session with an empty message
   role; the native importer returned `ok=false`, imported zero rows, and the
   adapter inventory proved that no partial session appeared.

The third trajectory mechanically proved that the actual model request saw the
imported user message, tool call ID, tool result, compaction summary, later user
message, and new follow-up. The loopback assistant response was appended to the
same native session and reparsed. After native compaction, the reader recovered
the new summary and accounted for archived rows.

One observed native nuance is documented rather than papered over: the first
CLI-created row stored a title but left `cwd` and `model` null despite launch
options, while imported sessions retained an explicit CWD. Both are valid
schema states, so inventory and parsing treat native CWD/model as optional.

## Test contract

The default credential-free suite covers bundle round trips, images,
tool-call/result linkage, compaction, private-reasoning omission, malformed
JSON and orphan rejection, active versus inactive projection, multi-session
inventory, implicit single-session selection, WAL snapshots, symlink rejection,
and schema-version rejection:

```bash
uv run pytest -q tests/test_hermes_format.py tests/test_hermes_native.py
```

The native test is opt-in because it requires an exact pinned source checkout
and its installed CLI. It still uses no provider credential and binds its fake
provider only to loopback:

```bash
SESSION_MIGRATE_HERMES_SOURCE=/path/to/hermes-agent-v2026.9.24 \
SESSION_MIGRATE_HERMES_BIN=/path/to/hermes-agent-v2026.9.24/.venv/bin/hermes \
  uv run pytest -q tests/test_hermes_native.py -vv
```

The test verifies the source commit before launching Hermes, creates a fresh
mode-`0700` home and workspace, imports the portable history, resumes it through
the actual CLI, inspects the provider-visible request, confirms the appended
turn, invokes native compaction, and reparses the result.
