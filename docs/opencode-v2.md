# OpenCode 2.0 transfers

`session-migrate import SOURCE --to opencode --target-cli /path/to/opencode`
now detects the installed CLI and selects its public transfer format. Legacy
1.17.20 continues using `opencode import/export --pure`; 2.0.x uses
`opencode session import/export --standalone`. Convert-only output remains
legacy by default; pass `--target-cli-version 2.0.23` to write a v2 bundle.
Other schema series are rejected for automatic import. Custom release suffixes
and the v2 `opencode v…` version prefix are accepted.

The v2 bundle uses session `location`/`model` and flat typed messages, including
native tool content and completed compaction summaries. Conversion reuses the
existing portable-event normalization and loss manifest. Text, linked tool
input/results, inline image attachments, and portable compaction summaries are
retained. Incomplete tool calls are archived as explicit imported errors,
because native v2 transfer drops unsettled assistant records. Their input is
retained and this change is recorded in the loss manifest. Encrypted checkpoints
and provider-private state are not portable and remain explicitly accounted for.
No private SQLite writes or source transcript changes are performed.
Free-form tool inputs are wrapped as `{\"input\": ORIGINAL_VALUE}` to satisfy
OpenCode's object input schema. The `tool_call:non_object_input` manifest counter
records this shape transformation, not an omitted call. String inputs remain
unchanged inside the wrapper, along with their call IDs and linked results.

Dry run performs native preflight without importing a session or writing a
migration manifest. The target CLI itself may initialize its ordinary local
store during preflight. Native import enforces global identity conflicts; the
adapter also checks existing IDs and requires the explicit native success
confirmation (v2 returns exit status zero even on conflict). Verification exports
the imported ID through the public CLI rather than relying on a project-filtered
session list.

For isolated validation, use a clean HOME and XDG_CONFIG_HOME/XDG_DATA_HOME/
XDG_CACHE_HOME/XDG_STATE_HOME, a credential-free configuration, and the actual
binary instead of a user wrapper that injects production credentials. Migration
manifests contain source hashes, the target release, conversion warnings, and
omission counts. Native database imports remain owned by OpenCode.

Validation currently targets the installed Linux gateway build of OpenCode
2.0.23, using synthetic transcripts and no paid model requests. Windows native
validation is separate; Linux tests do not establish Windows runtime behavior.
