# Antigravity Desktop Session Interoperability Specification

This document provides a clean-room technical specification of the session storage architecture, SQLite schemas, Protobuf wire formats, Connect-RPC interfaces, and migration mechanics for **Antigravity Desktop**, complementing the research published in [antigravity-session-interoperability](https://github.com/xhluca/antigravity-session-interoperability).

---

## 1. Pinned Host Environment & Binary Artifacts

All findings were empirically derived from isolated test environments and installer package analysis across the official distributions of Antigravity Desktop 2.18.1:

| Platform | Installer / Package | Language Server Binary | Size (bytes) | SHA-256 |
|---|---|---|---|---|
| **macOS (arm64)** | DMG (`Antigravity.app`) | `Contents/Resources/bin/language_server` | `148,990,608` | `300ee20f3108a511be1149602b91ba84cbbdcc42213067151e255584129be30f` |
| **Linux (x64)** | `Antigravity.tar.gz` | `<install>/resources/bin/language_server` | `181,432,528` | `ab4937445fa3817bc374a1db71062de7daecdb093cbfb6bd36b80f443198670e` |
| **Windows (x64)** | NSIS (`Antigravity-x64.exe`) | `<install>\resources\bin\language_server.exe` | `163,640,320` | `1ed84e6a1d1e51064d80c9f382ab3a519eb2775cb91d552c63064a18cfdf3cf2` |

- **App Version**: `2.18.1` (`package.json`, `dist/main.js`), Electron `44.3.0`.
- **Application Data Directory**: `<home>/.gemini/antigravity` across all platforms.
- **Conversation Storage**: `<home>/.gemini/antigravity/conversations/<conversation_uuid>.db`
- **Summary & Index Store**: `<home>/.gemini/antigravity/conversation_summaries.db` (`PRAGMA user_version = 3`)
- **Shared JavaScript**: Static package analysis confirms `resources/app.asar` (SHA-256 `3c03ce352dc3c1b43f357c27ead1f73c74d198a8ded89b1b3d6a2539e7a6ac5c`) is byte-identical across all three installers, sharing the same `languageServer.js` launcher and arguments across `darwin`, `linux`, and `win32`.

No vendor binaries, proprietary descriptors, or user conversation logs are reproduced in this repository; all schema definitions were established through clean-room differential probing and synthetic round-trip verification.

### Platform Support Status

- **Database & Wire Protocol (Cross-Platform)**: The SQLite schemas (`conversations/<uuid>.db`, `conversation_summaries.db`), Protobuf wire serialization, UUID structures, and RFC 3339 timestamps are OS-agnostic and identical across macOS, Linux, and Windows.
- **macOS (`darwin-arm64`)**: Fully verified and validated end-to-end. Includes live language server subprocess execution, Connect-RPC verification (`GetCascadeTrajectorySteps`), and sidebar discovery validation.
- **Linux & Windows**:
  - Standard Electron path resolution is implemented:
    - User configuration & `app_storage.json`: Linux uses `$XDG_CONFIG_HOME/Antigravity` (default `~/.config/Antigravity`); Windows uses `%APPDATA%\Antigravity`.
    - Language server binary: Linux searches PATH, common `/opt/Antigravity/resources/bin/language_server`, and `~/.local/share/`; Windows searches `%LOCALAPPDATA%\Programs\antigravity\resources\bin\language_server.exe` and `%PROGRAMFILES%`.
  - All three platforms are pinned by exact file size and SHA-256 checksum in `PINNED_DESKTOP_SPECS`.
  - Live native oracle execution on Linux/Windows remains pending live installation confirmation, but binary verification and path resolution are active for all three platforms.

---

## 2. Desktop vs CLI Architectural Differences

While both Antigravity CLI (`agy`) and Antigravity Desktop (`Antigravity.app`) share an underlying SQLite + Protobuf storage engine, their database schemas and wire properties exhibit distinct constraints:

| Property | Antigravity CLI (`antigravity`) | Antigravity Desktop (`antigravity-desktop`) | Rationale / Mechanism |
|---|---|---|---|
| **App Data Subdirectory** | `~/.gemini/antigravity-cli` | `~/.gemini/antigravity` | Passed via `-app_data_dir=antigravity` flag to `language_server` |
| **`trajectory_meta.source`** | `17` (`CLI`) | `1` (`IDE` / `Hub`) | Source identifier enum in metadata table |
| **`trajectory_metadata_blob` Project ID** | `"default-cli-project"` | `"outside-of-project"` (or workspace UUID) | Project boundary discriminator (Protobuf field 18) |
| **`conversation_summaries.source`** | `"antigravity-cli"` | `""` (empty string) | Desktop UI filter expects empty source string |
| **`conversation_summaries.app_data_dir`** | `"antigravity-cli"` | `"antigravity"` | Tells the IDE process where to resolve conversation DBs |
| **`conversation_summaries.status`** | `""` (empty string) | `"CASCADE_RUN_STATUS_IDLE"` | Desktop UI cascade lifecycle state |
| **`conversation_summaries.project_id`** | `"default-cli-project"` | `"outside-of-project"` | Project grouping in desktop sidebar |
| **Summary Schema Columns** | 19 or 21 columns | 21 columns (includes `raw_summary` BLOB, `group_id` TEXT) | Extended schema in `user_version = 3` |
| **Subagent Trajectory Roots** | Root ID == Conversation ID | Root ID == Root Cascade ID (nesting depth $\ge 1$) | Subagent conversations reference their parent root |
| **Extended Step Statuses** | `3` (`DONE`), `7` (`ERROR`) | `3` (`DONE`), `5` (`CLEARED`), `7` (`ERROR`) | Status 5 occurs when user clears a conversation step |

---

## 3. SQLite Database Layout

### 3.1 Conversation Database (`conversations/<uuid>.db`)

Each conversation is stored as an independent, single-file SQLite 3 database with WAL mode enabled. It contains 7 tables:

```sql
CREATE TABLE trajectory_meta (
    trajectory_id TEXT,
    conversation_id TEXT,
    trajectory_type INTEGER,
    source INTEGER
);

CREATE TABLE steps (
    idx INTEGER PRIMARY KEY,
    step_type INTEGER NOT NULL DEFAULT 0,
    status INTEGER NOT NULL DEFAULT 0,
    has_subtrajectory NUMERIC NOT NULL DEFAULT false,
    metadata BLOB,
    error_details BLOB,
    permissions BLOB,
    task_details BLOB,
    render_info BLOB,
    step_payload BLOB,
    step_format INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE gen_metadata (
    idx INTEGER PRIMARY KEY,
    data BLOB,
    size INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE executor_metadata (
    idx INTEGER PRIMARY KEY,
    data BLOB
);

CREATE TABLE parent_references (
    idx INTEGER PRIMARY KEY,
    data BLOB
);

CREATE TABLE trajectory_metadata_blob (
    id TEXT PRIMARY KEY DEFAULT "main",
    data BLOB
);

CREATE TABLE battle_mode_infos (
    idx INTEGER PRIMARY KEY,
    data BLOB
);
```

#### Table Constraints & Validation Rules:
1. `trajectory_meta`: Exactly 1 row.
   - `trajectory_type`: `4` (Cascade).
   - `source`: `1` for Desktop (vs `17` for CLI).
2. `steps`: `idx` must be a contiguous 0-indexed sequence (`0, 1, 2, ...`).
   - The columns `step_type` and `status` must strictly match the inner Protobuf wire fields 1 and 4 of `step_payload`.
3. `trajectory_metadata_blob`: Exactly 1 row with `id = 'main'`.

### 3.2 Summary Catalog (`conversation_summaries.db`)

Antigravity Desktop maintains a shared index database at `~/.gemini/antigravity/conversation_summaries.db`:

```sql
CREATE TABLE conversation_summaries (
    conversation_id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT "",
    preview TEXT NOT NULL DEFAULT "",
    step_count INTEGER NOT NULL DEFAULT 0,
    last_modified_time DATETIME NOT NULL,
    workspace_uris TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT "",
    source TEXT NOT NULL DEFAULT "",
    project_id TEXT NOT NULL DEFAULT "",
    agent_name TEXT NOT NULL DEFAULT "",
    parent_conversation_id TEXT NOT NULL DEFAULT "",
    nesting_depth INTEGER NOT NULL DEFAULT 0,
    battle_id TEXT NOT NULL DEFAULT "",
    winning_conversation_id TEXT NOT NULL DEFAULT "",
    not_fully_idle NUMERIC NOT NULL DEFAULT false,
    killed NUMERIC NOT NULL DEFAULT false,
    last_user_input_time DATETIME NOT NULL,
    last_user_input_step_index INTEGER NOT NULL DEFAULT -1,
    app_data_dir TEXT NOT NULL DEFAULT "",
    raw_summary BLOB,
    group_id TEXT NOT NULL DEFAULT ""
);

CREATE INDEX idx_conversation_summaries_last_user_input_time
    ON conversation_summaries(last_user_input_time);
CREATE INDEX idx_conversation_summaries_last_modified_time
    ON conversation_summaries(last_modified_time);
```

---

## 4. Protobuf Wire Encoding

Antigravity uses Protobuf 3 wire serialization inside BLOB columns. `session-migrate` implements a bounded zero-dependency wire decoder and encoder.

### 4.1 Step Envelope (`step_payload`)

| Field Number | Wire Type | Type / Description | Meaning |
|---|---|---|---|
| `1` | Varint | `uint32` | Step Type Enum: `14` (User Input), `15` (Planner Response), `132` (Generic Tool Result) |
| `4` | Varint | `uint32` | Step Status Enum: `3` (Done), `5` (Cleared), `7` (Error) |
| `19` | Length-delimited | Sub-message | User input payload (when type == 14) |
| `20` | Length-delimited | Sub-message | Planner response payload (when type == 15) |
| `140` | Length-delimited | Sub-message | Generic tool payload (when type == 132) |

### 4.2 User Input Payload (Field 19)

| Field Number | Wire Type | Meaning |
|---|---|---|
| `2` | Length-delimited | Raw prompt / user message text string |

### 4.3 Planner Response Payload (Field 20)

| Field Number | Wire Type | Meaning |
|---|---|---|
| `1` | Length-delimited | Assistant message response text string |
| `6` | Length-delimited | Message UUID |
| `7` | Length-delimited | Repeated sub-message: Tool Call |

#### Tool Call Sub-message (Field 7):
| Field Number | Wire Type | Meaning |
|---|---|---|
| `1` | Length-delimited | Call ID UUID string |
| `2` | Length-delimited | Tool function name (e.g. `view_file`, `run_command`) |
| `3` | Length-delimited | JSON-encoded function arguments |

### 4.4 Trajectory Metadata Blob (`trajectory_metadata_blob.data`)

This blob contains a serialized `exa.cortex_pb.CortexTrajectoryMetadata` message:

| Field Number | Wire Type | Field Name | Meaning |
|---|---|---|---|
| `1` | Length-delimited | `workspaces` | Repeated `CortexWorkspaceMetadata` sub-messages |
| `2` | Length-delimited | `created_at` | Sub-message: Creation timestamp (Field 1: seconds varint, Field 2: nanos varint) |
| `3` | Length-delimited | `initialization_state_id` | Initialization state UUID |
| `5` | Length-delimited | `parent_conversation_id` | Parent conversation UUID (for subagents) |
| `6` | Length-delimited | `root_conversation_id` | Root cascade UUID string |
| `7` | Length-delimited | `workspace_uris` | Repeated workspace file URIs (`file:///...`) |
| `17` | Varint | `nesting_depth` | Subagent nesting depth (`0` for root conversation) |
| `18` | Length-delimited | `project_id` | Project UUID or `"default-cli-project"` |

---

## 5. Hub Summaries & Project Scoping Architecture

### 5.1 Project Scoping and Sidebar Rendering

Unlike the CLI (which defaults all conversations to `"default-cli-project"`), Antigravity Desktop organizes all conversations strictly under **Projects**:

1. **Active Projects List**: Tracked in `~/Library/Application Support/Antigravity/app_storage.json` under the key `projectsOrder` (JSON array of project IDs).
2. **Project Definitions**: Stored in `~/.gemini/config/projects/<project_id>.json`. Each project defines its `id`, `name`, and `projectResources.resources` (containing `folderUri` or `gitFolder.folderUri`).
3. **Sidebar Rendering Invariant**: The desktop sidebar only renders sections for project IDs present in `projectsOrder`. Conversations with `project_id = "outside-of-project"` have no header in the desktop UI and are excluded from sidebar listings and project-scoped search.
4. **Resolution Strategy**:
   - When importing a session, check if the session's workspace `cwd` matches an existing project definition in `~/.gemini/config/projects/*.json`.
   - If a matching project is found, assign its `id`.
   - If no project matches, assign `project_id = "default-cli-project"` (which corresponds to `"CLI Project"`, present in `projectsOrder`).
   - Alternatively, a new project definition can be written to `~/.gemini/config/projects/<uuid>.json` and added to `projectsOrder` in `app_storage.json`.

### 5.2 Summary Stores (`conversation_summaries.db` & `agyhub_summaries_proto.pb`)

Antigravity Desktop maintains two synchronized summary stores:

1. **SQLite Summary Table** (`conversation_summaries.db`):
   - Table `conversation_summaries` (21 columns in schema v3).
   - Column `raw_summary` (BLOB): Contains the binary protobuf-encoded `CascadeTrajectorySummary`.
2. **Hub Summaries Protobuf File** (`agyhub_summaries_proto.pb`):
   - A single binary file storing `map<string, CascadeTrajectorySummary>`.
   - Wire format: Repeated field `1` containing sub-message:
     - Field `1` (string): `conversation_id`
     - Field `2` (bytes): Serialized `CascadeTrajectorySummary`
   - Serves as the high-speed in-memory cache for the Electron sidebar and quick-picker.

### 5.3 `CascadeTrajectorySummary` Wire Specification

```protobuf
message CascadeTrajectorySummary {
  string summary = 1;                              // Conversation title
  uint32 step_count = 2;                           // Number of active steps
  google.protobuf.Timestamp last_modified_time = 3;// Last modification timestamp
  string trajectory_id = 4;                        // Trajectory UUID
  CascadeRunStatus status = 5;                     // 1 = CASCADE_RUN_STATUS_IDLE
  google.protobuf.Timestamp created_time = 7;      // Creation timestamp
  repeated CortexWorkspaceMetadata workspaces = 9; // Workspace folder URIs
  google.protobuf.Timestamp last_user_input_time = 10;
  ConversationAnnotations annotations = 15;        // Title and metadata annotations
  uint32 last_user_input_step_index = 16;          // Index of last user message
  CortexTrajectoryMetadata trajectory_metadata = 17;// Full trajectory metadata (includes project_id)
  CortexTrajectorySource source = 22;              // 4 = IDE/Desktop Cascade
}
```

---

## 6. Native Language Server Connect-RPC Protocol

Antigravity Desktop's Electron frontend communicates with the background Go native process (`language_server`) via Connect-RPC over HTTPS.

### 6.1 Invocation Arguments

```bash
/Applications/Antigravity.app/Contents/Resources/bin/language_server \
  --standalone \
  --override_ide_name antigravity \
  --subclient_type hub \
  --override_ide_version 2.18.1 \
  --override_user_agent_name antigravity \
  --https_server_port 0 \
  --csrf_token <session_csrf_token> \
  -gemini_dir ~/.gemini \
  -app_data_dir antigravity
```

- When `--https_server_port 0` is passed, the server binds to a random loopback port and logs:
  `Language server listening on random port at <PORT> for HTTPS (gRPC)`
- The server expects `stdin` to be closed immediately upon spawn (unless in WSL liveness mode).

### 6.2 Trajectory Inspection RPC

- **Method**: `POST https://127.0.0.1:<PORT>/exa.language_server_pb.LanguageServerService/GetCascadeTrajectorySteps`
- **Headers**:
  - `Content-Type: application/json`
  - `X-Codeium-Csrf-Token: <session_csrf_token>`
- **Request Body**:
  ```json
  {"cascadeId": "<conversation_uuid>"}
  ```
- **Response Structure**:
  ```json
  {
    "steps": [
      {
        "type": "CORTEX_STEP_TYPE_USER_INPUT",
        "status": "CORTEX_STEP_STATUS_DONE",
        "userInput": {"userResponse": "..."}
      },
      {
        "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
        "status": "CORTEX_STEP_STATUS_DONE",
        "plannerResponse": {
          "response": "...",
          "messageId": "..."
        }
      }
    ]
  }
  ```

---

## 7. Migration Mechanics & Collision Guarantees

In `session-migrate`, transferring sessions to Antigravity Desktop adheres to strict invariants:

1. **Source Preservation**: The source files in `~/.gemini/antigravity-cli/` are opened read-only and never modified, truncated, or removed.
2. **Fresh UUID Generation**: By default, `transfer` and `import` generate fresh non-colliding UUID4s for both the conversation ID and trajectory ID, eliminating any possibility of overwriting existing desktop conversations.
3. **Collision Detection**: If an explicit `--session-id` is specified that already exists in either `conversations/<id>.db` or `conversation_summaries.db`, `session-migrate` immediately halts with a collision error.
4. **Atomic Installation**: Conversation databases are created in temporary staging directories with `0o600` permissions and moved into place via atomic file operations. Summary rows are inserted with collision checks using standard SQLite transaction semantics.
5. **Dry-Run Capability**: Running with `--dry-run` performs full validation, model mapping, and schema formatting without touching the destination filesystem.
