# OpenCode 2.0 public session transfers

OpenCode 1.17.20 uses `import/export --pure` and nested message/part bundles.
OpenCode 2.0 uses `session import/export --standalone` and flat typed messages.
Native import detects the installed client and selects its transfer schema.
Convert-only output remains legacy unless `--target-cli-version 2.0.23` is set.
Unsupported schema series fail closed. The `opencode v` version prefix and
custom suffixes are accepted; unvalidated exact releases receive schema-aware
warnings. Native acceptance is pinned to stock 2.0.23, not every 2.0.x release.

The v2 adapter preserves ordered user/assistant text, linked tool inputs and
results, inline user/tool images, and readable completed compaction summaries.
User text blocks in one native message become their exact newline join.
Private-only or whitespace-only checkpoints do not retire readable history.
Encrypted provider checkpoints, private traces, source reasoning variants and
unsupported control/metadata fields are explicitly counted as omissions.

A streamed tool input can be an incomplete JSON string. The source projection
keeps it unchanged inside `{ "input": ORIGINAL_STRING }`; it does not replace
it with `{}` or attempt to parse/complete it. On conversion, an unfinished call
is archived as an `ImportedIncompleteTool` error with the same ID and input,
because native transfer discards unsettled assistant records. This is historical
input preservation, not an executable resumed partial tool invocation. The
source→portable→target regression includes an unterminated string input.

OpenCode owns its database import. No private SQLite writes, credentials or
machine-specific launchers are part of the adapter. Dry run performs public
native preflight without importing or writing a migration manifest; native
preflight may initialize the client's ordinary store. Imports enforce global
identity collisions and explicit success confirmation, then verify the imported
ID using public export. A conflict can have native exit status zero and still
fails import confirmation.

## Reproducible native and route checks

```sh
./scripts/install-native-test-clis.sh /tmp/session-migrate-native opencode-v2
./scripts/run-native-test-client.sh \
  opencode-v2 /tmp/session-migrate-native/session-migrate-native.env
uv run pytest -q tests/test_opencode_v2.py tests/test_route_matrix.py
```

The `opencode-v2` CI matrix entry installs **@opencode/cli@2.0.23** (the legacy
package is opencode-ai), supplies `SESSION_MIGRATE_TEST_OPENCODE_V2`, and rejects
skipped assigned tests. Native tests use synthetic transcripts, isolated HOME
and XDG directories, an explicit credential-free config, and a deterministic
localhost provider. They verify import/export, dry run, collisions, cold process
reopening, compaction/tool replay and a new persisted reply under the same ID.
They do not use real accounts or supplier inference credits. The default suite
can skip native tests when that exact executable is unavailable; the selected
CI job cannot silently pass those skips.

Route tests cover legacy and v2 OpenCode source and target variants against the
other supported formats. The 19 variants remain eighteen harnesses. Existing
legacy wire behavior and native tests remain separate.

This change is independent of the Windows filesystem-cleanup and Codex child
projection changes split from PR #10. Those fixes complement Windows execution
and additional Codex sources, but neither is required to review this schema or
run the Linux v2 gate. Native Windows acceptance and all historical client
versions are not established by this branch's Linux gates.
