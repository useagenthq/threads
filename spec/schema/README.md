# spec/schema

The wire contract for the threads log. Both implementations read and write exactly these bytes.

| File | What it is |
|---|---|
| `events.v1.schema.json` | JSON Schema (draft 2020-12) for one canonical line of a branch: the `Header`, an event (envelope plus per-type `data`), or the `Head` checkpoint |
| `host-api/` | The host HTTP API: `openapi.json` (routes) and `host-api.v1.schema.json` (bodies and projections, `$id` `urn:threads:schema:host-api:v1`) |
| `api.schema.json` | The shape of `../api.json`, the public API contract |
| `store.sql` | The normative SQLite DDL of the log store, versioned by `PRAGMA user_version`. Embedded in both implementations by `../tools/gen_store_sql.py` (`--check` in CI) |

## Ownership

One schema, three forms, with a single author.

1. **Authored in Zod 4** in `typescript/` (the log module). The TS types are `z.infer` of it.
2. **Exported** with `z.toJSONSchema` into `spec/schema/events.v<N>.schema.json`, and committed.
3. **Generated** into Pydantic v2 models in `python/` from the committed JSON Schema, with pinned generator settings that never emit `Any`. Arbitrary JSON (tool input, model params, MCP payloads) is the recursive `JsonValue` type, not `Any`.

CI regenerates both steps and fails on any diff. Nobody hand-edits the exported file or the generated Python.

**Bootstrap:** the current `events.v1.schema.json` was written by hand, before any Zod exists. The first TypeScript PR writes the Zod source so that it reproduces this file's accepted and rejected values. The conformance corpus and the negative cases are the check. From then on the file is generated only.

Rules that JSON Schema can't express, or that don't survive `z.toJSONSchema` (Zod refinements aren't exported), are allowed only when they are listed under [Semantic rules](#semantic-rules) with a conformance case. Both languages implement them by hand.

## One contract for storage and interchange

SQLite is the storage engine. JSONL is the interchange, export and conformance format. They hold **the same bytes**:

- Each branch stores its header line and its own event lines, byte for byte as written (`branches.header_line`, `events.line`). The head checkpoint `(seq, hash)` is stored in `branches` and updated in the same transaction as every append.
- `threads export <branch>` writes those stored lines, one per line with `\n`, followed by the `Head` line. Import verifies every line, the chains and the head, then stores the same bytes.
- The hash chain is over those bytes. A fixture that verifies as JSONL verifies in SQLite, and the reverse.

## Wire rules

1. **snake_case on the wire,** in both languages. Language APIs may re-case accessors (`forkPoints` ↔ `fork_points`), but stored and hashed bytes never change.
2. **Lines.** Each line is one JSON object in **RFC 8785 (JCS) canonical form**. The maximum line is 1 MiB; anything larger goes in an artifact.
   - Writers emit JCS, so a Python writer and a TS writer produce identical bytes for the same event.
   - **Admission:** a reader admits a line only if its bytes equal the RFC 8785 serialization of its parsed value. Anything else (`1e3`, `1000.0`, `-0`, keys out of order, whitespace, a non-minimal or uppercase `\u` escape, `\/`) is `invalid_line`. This runs after strict JSON parsing and before format admission (the header and head rule below) and the schema (`line-noncanonical-*`).
   - **Nesting:** a line nests at most **64** levels of arrays and objects (the line object is level 1). A deeper line is `invalid_line`, checked while parsing so no stage depends on stack depth (`line-nesting-too-deep`).
   - Readers hash the **raw bytes as stored** and never re-serialize to compute or verify a hash. The admission check compares bytes; it never replaces them.
3. **Hashes.** All are lowercase hex SHA-256.

   | Field | Input bytes | Domain |
   |---|---|---|
   | `prev_hash` | the previous line in the same segment, without `\n`; the first event of a segment hashes its header | raw |
   | `Head.hash` | the branch's last line (its header when `seq` is 0) | raw |
   | `fork.at_hash` | the parent's line at `at_seq` | raw |
   | `request_ref.sha256` (= `req_hash`), `declared_prefix.sha256` | [Render v1](#render-v1) bytes | raw |
   | `ArtifactRef.sha256` | the artifact's bytes, full 64 hex digits | raw |
   | `config_hash`, `args_hash`, `manifest_hash`, `tools_changed.tools_hash` | RFC 8785 bytes of the structured value | canonical |

4. **Numbers.** Envelope fields and framework-authored integer fields are integers in `0 … 2^53−1`. Floats appear only in `JsonValue` positions (tool `input`, `model_params`, adapter `settings`), spelled per . Fractions in policy are integer permille; prices are integer nano-units per token. Token counts are an integer or `null` (unknown, never zero; ).
5. **Identifiers.** `thread_id`, `branch_id` and `event_id` are lowercase UUID strings, branded per kind in both languages. Writers MUST generate UUIDv7. Readers accept any lowercase UUID: the version is a writer rule, not an admission rule. `call_id` is opaque and may come from the provider.
6. **Pinned names.** Event `type` names, every enum literal and every `ErrorCode` are frozen by a golden test in both languages. Add only. Never update the pins in a rename PR.
7. **Header** (a branch's first line): `{format: "threads.log", format_version: 1, thread_id, branch_id, created_at, writer: {impl, version}}`. It is not an event: no `seq`, no `prev_hash`. Every branch, root or child, has its own header. `writer` pins the only implementation and major version that may append.
8. **Head checkpoint** (the last line of every export): `{format: "threads.head", format_version: 1, branch_id, seq, hash}`.
   - **Format admission** (header and head lines), checked before the line's schema: if `format` is `threads.log` or `threads.head` and `format_version` is an integer greater than 1, the error is `unsupported_format`: a newer writer, not corruption. A missing `format_version`, a non-integer (`"1"`, `1.5`), zero or a negative number, or a `format` that is not one of those two strings is `invalid_line` (`header-format-array`). The error `seq` is 0 for the header and the checkpoint's `seq` for the head (`header-format-version-*`, `head-format-version-*`, `header-format-unknown`).
9. **Envelope** (every event):

   | Field | Rule |
   |---|---|
   | `seq` | Contiguous along the branch's resolved chain. The first event after a root header is 1 |
   | `event_id` | Unique along the resolved chain: no event of this branch reuses an id from its own segment or any ancestor segment (semantic rule 28). All cross-event references use it |
   | `thread_id`, `branch_id` | `branch_id` is the branch that wrote the event, which is always its segment's header `branch_id` |
   | `epoch` | The writer's lease epoch. Non-decreasing along the resolved chain |
   | `type`, `type_version` | Discriminant plus per-type schema version |
   | `time` | Epoch ms. Informational only, never used for ordering |
   | `actor` | `{kind, principal?}`. `principal` is required on `user_input`, `steer`, `channel_delivery`, `cancel_requested`, `stop_when_idle`, `approval_granted`, `approval_denied`, `permission_rule_added` and `tool_result{origin: answered}` |
   | `prev_hash` | See the hashes table |
   | `critical` | Always present. See below |
   | `data` | Strict per-`(type, type_version)` payload. Unknown keys are rejected |

10. **Critical vs ignorable.** Every event states `critical` explicitly. For known types the value is pinned in the schema.
    - A reader that doesn't know an event's `(type, type_version)` **refuses the whole log** with `unsupported_critical_event` if the event is critical. This means a newer writer, not corruption.
    - If the unknown event is not critical, the reader keeps it: `reduce` skips it, but the head seq and hash still advance.
    - New types default to `critical: true`. Use `false` only when skipping the event cannot change `reduce` or render output (`log_repaired`, `park_escalated`, `schedule_skipped`). An event that execution reads is critical even if render ignores it: `retry_scheduled` (recovery reads `not_before`, and the wait budget counts `delay_ms`).
11. **Epoch fencing.** Epochs come from the branch lease. Acquiring a lease sets `epoch = max(lease epoch, max epoch in the resolved chain) + 1`, and every append by that holder, recovery included, uses it. A writer, gateway or adapter holding a lower epoch than the lease is stale and rejects with `stale_epoch` before dispatching anything. Fencing stops our own dispatch path. It never proves that an earlier dispatch did not happen.
12. **Branches, segments and fork.**
    - A root branch is one segment: its header, then events from `seq` 1.
    - A child branch starts with its own header, then a `fork` event at `seq = at_seq + 1`. The fork's `prev_hash` is the hash of the child's header, and `fork.at_hash` is the hash of the parent's line at `at_seq`. The **resolved chain** is the parent's resolved chain through `at_seq`, then the child's own events.
    - Parent rows are **referenced, never copied or rewritten**. SQLite stores only the child's own lines. An export of a child writes each ancestor segment (its header and its lines through the fork point, byte-identical), then the child segment, then the child's `Head`.
    - A parent branch can't be deleted while a child references it (`branch_has_children`). Deleting a thread deletes all its branches together.
    - `fork.reason` is `snapshot` for a user fork at an eligible snapshot event or `repair` for operator repair of a corrupt parent (item 14).
    - A `snapshot` fork records `knowledge_policy`: `pinned` (the API default) searches knowledge as of the fork snapshot's `knowledge_revision`; `current` searches the live corpus. A `repair` fork has neither a sandbox nor a knowledge policy.
    - A `repair` child is **inspection-only**: it reduces, renders and exports, but it is never runnable, because no sandbox matches its log. Acquiring it to run fails with `branch_not_runnable` and appends nothing. To continue work, fork it at an eligible snapshot in its resolved chain.
13. **Derived, never stored.** Payloads carry no field that the envelope or an earlier event already fixes:

    | Value | Derivation |
    |---|---|
    | effect key | `"<branch_id of the tool_call event>:<call_id>"`. Stable across re-dispatch; a new branch's new calls get new keys automatically. Sent to providers as the idempotency key |
    | `args_hash` of a call | SHA-256 of the RFC 8785 bytes of `tool_call.input` |
    | a call's `effect_class`, `dedup_window_ms` | The `ToolSpec` with that `name` in the latest tool set before the call: the last `tools_changed`, else `thread_started.tools` |
    | `req_hash` | `model_request.request_ref.sha256` |
    | a snapshot's captured position | Everything before the snapshot event. The snapshot event is the fork point |
    | `fork` at_seq and thread | `at_seq` = the fork event's `seq − 1`. The thread is the envelope `thread_id` (forks never cross threads) |
    | an approval's branch | The envelope `branch_id` |
    | a request's settings epoch and line 0 | The latest `thread_started` or `settings_changed` before it |
    | the loaded tool set | The latest `tools_changed` (specs without `defer_loading`), else `thread_started.tools` |
    | the compaction circuit breaker | `compaction_failed` events since the last `compacted` ≥ `compact.max_failures` |
    | cost, usage totals, cache breaks | Projections: one cost disposition per `model_request` (response usage, proven not billed, or the model-declared bound), priced from `policy.models` |
    | budget reservations | The host `budget_ledger`, a durable cache of the tree's logs, rebuilt on restart |
    | the permission mode | `policy.permissions.mode`, then the latest `mode_changed` |
    | todo list, children, team tasks | The latest `todos_updated`; `agent_spawned` / `agent_finished`; the `team_task_*` events |
    | a pinned fork's knowledge revision | The fork snapshot's `knowledge_revision` (the parent's snapshot event at `at_seq`) |
    | the owner of a `budget_exceeded` with scope `run` or `thread` | The envelope `thread_id`. Only scope `ancestor` names `owner_thread_id`, and there it is required |

14. **Integrity scope.**
    - The chain detects any change to a line before the last one: the next line's `prev_hash` stops matching.
    - A change to the last line, or removal of a valid suffix, is detected only against the committed head checkpoint (`head_mismatch`). SQLite keeps the checkpoint in the same transaction as the rows. An export carries it as its last line.
    - Neither is a signature. Anyone who can rewrite both the lines and the checkpoint can forge a consistent log. v0.1 claims detection of corruption, bugs and truncated copies, not tamper-proofing.
    - **Corruption at `seq k`** refuses writable opens with `log_corrupt{at_seq, cause}`, where `cause` is the verifier's precise code for that line. `log_corrupt` is the status of a stored branch; conformance cases and import report the precise code. Readers get the valid prefix plus a flag. Nothing is truncated in place. `threads repair <branch>` is an explicit operator command: it creates a new child with `fork{reason: repair}` at `k − 1` and marks the corrupt branch read-only. The corrupt rows stay as evidence, so the log stays append-only.
    - **Torn tails exist only in JSONL** (an interrupted copy). Import serves the valid prefix, flags the head as unverified, keeps the dropped bytes as an artifact, and the imported branch records `log_repaired{truncated_bytes, at_offset, dropped_ref}`. **Newline is the commit marker:** every exported line ends in `\n`, and only the unterminated final chunk can be torn. It is dropped whatever it holds, even a line that parses and verifies (`torn-tail-head-unterminated`, `torn-tail-wrong-head-unterminated`). Every `\n`-terminated line must be valid: import fails at that line with the verifier's precise code (`invalid_line`, `unsupported_format`, `seq_mismatch`, `prev_hash_mismatch`, ...), never as a torn tail, and a terminated head that doesn't match is `head_mismatch` (`head-checkpoint-suffix-removed`).

## Render v1

The one normative representation of a model request. `req_hash` covers these bytes, and the adapter guard re-renders and re-hashes before every dispatch (`request_hash_mismatch` on any difference). It is JSONL: each line is JCS followed by `\n`. The generator's reference implementation is `spec/tools/fixtures/render.py`.

- **Line 0 is the declared prefix of the current settings epoch:** `{adapter, model, params, system, tools}`.
  - `system` and `tools` come from `thread_started` and never change.
  - `model`, `params` (`model_params`) and `adapter` come from the latest `thread_started` or `settings_changed` before the request.
  - `adapter` is the model adapter's name, version and every provider-conversion setting, including declared hosted tools.
  - `tools` is `[{name, description, input_schema}]` in declared order. A spec with `defer_loading: true` renders as `{name, description, deferred: true}`. `effect_class`, `dedup_window_ms`, `output_schema` and `ends_turn` aren't model-visible and are omitted.
- **Framing escape.** Every framing wrapper (`<reference>`, `<context>`, `<heartbeat>`, and the summary reference) escapes each attribute value and its whole body text with `esc`: replace `&` with `&amp;` first, then `<` with `&lt;`, `>` with `&gt;`, `"` with `&quot;` and `'` with `&#39;`. Only the wrapper's own tags are raw, so stored text (a memory id, a memory body, a summary, a call id) can never close a wrapper or open another one. The escape applies to the rendered text only: artifacts and event payloads keep their original bytes (`render-reference-framing-escaped`).
- **Then one line per model-visible event, in log order:**

  | Event | Line |
  |---|---|
  | `user_input`, `steer` | `{"role":"user","content":[{"type":"text","text":<text>}]}`, or `{"role":"user","content":<content>}` when the event has `content` (ordered input parts, as recorded). **Nothing** if a later `hook_decision{hook: before_input, decision: deny \| failed, input_event_id}` before the request names it |
  | `injected`, trust `untrusted_reference` | A user line whose text is `<reference source="esc(S)" id="esc(ID)" untrusted="true">\nesc(TEXT)\n</reference>` |
  | `injected`, trust `trusted_instruction` | A user line whose text is `<context source="esc(S)" id="esc(ID)">\nesc(TEXT)\n</context>` |
  | `heartbeat` | A user line whose text is `<heartbeat>\nrunning: esc(ID), esc(ID)\n</heartbeat>` |
  | `model_response`, `model_response_recovered` | `{"role":"assistant","content":<content>}`, parts as recorded. `reasoning` and `hosted_tool` parts of a response recorded before a `settings_changed{reasoning_carryover: omit_prior}` are omitted. If nothing is left, no line. **Nothing** when the response answers a `model_request{purpose: compaction}` |
  | `tools_changed` | `{"role":"tools","tools":[…]}`: the complete new set, rendered as in line 0 (deferred specs as stubs). Line 0 is unchanged |
  | `tool_result` | `{"role":"tool","call_id":…,"is_error":…,"content":<parts>}`. `<parts>` is `[{"type":"text","text":<preview>}]` when the event has no `content`, else `content` as recorded (the preview is then not rendered). A `context_edited` before the request changes it: `clear` makes `<parts>` exactly `[{"type":"text","text":"[tool result cleared: call_id=<id>; read it with read_tool_result]"}]`; `redact` replaces each span (UTF-8 bytes) of text part `part` with `[redacted]` |
  | `tool_result_late` | The same, plus `"late":true`. The earlier placeholder line stays |
  | `compacted` | The lines of events `from_seq..to_seq` are dropped (a compacted event inside a later range is dropped too). In place of the range's first event go: one user line, the summary artifact's text wrapped (escaped) as a reference with `source="summary"` and `id=<summary sha256>`, then the line of the **last** `tools_changed` inside the range, if any, so loaded and changed tools survive. The compacted event itself renders nothing. Line 0 is never compacted |

  All other events render nothing (`model_request`, `settings_changed`, `context_edited`, `context_preflight_blocked`, `hook_decision`, …).
- **Artifacts.** Parts carry artifact refs, not bytes. An event whose text is in an artifact renders the verified bytes decoded as UTF-8. Before dispatch every artifact referenced by a rendered part is read and hash-verified. A missing or corrupt artifact is `artifact_missing` / `artifact_corrupt`, never substituted, and the request isn't sent (`render-image-artifact-missing`).
- **Compaction side request** (`model_request{purpose: compaction}`, ): the normal render of the events before it, then one user line whose text is the fixed instruction `Summarize the conversation so far for your own continuation. Keep the user's goals and constraints, decisions made, files and identifiers touched, open tasks with their status, and the next step. Reply with the summary only.` followed, when `before_compact` hooks returned `guide` decisions since the previous `model_request`, by `\n\nAdditional instructions:\n` and their reasons joined by `\n`. Its line 0 is the epoch's line 0, so C7 holds.
- **Provider wire bytes** are derived deterministically from these bytes and the verified artifacts by the adapter that line 0 names, using only the settings line 0 records. So every adapter-visible setting is inside the hashed bytes. An adapter that can't encode a rendered part fails before dispatch with `content_unsupported` or `continuation_unsupported`.
- **C7, the declared prefix, per settings epoch.** `model_request.declared_prefix` describes line 0 (bytes including its `\n`, and SHA-256). Within one settings epoch every recorded `declared_prefix` must be **equal** to every other and to that epoch's line 0 as re-rendered. A prefix that changes without a `settings_changed`, including one that grows while keeping its old bytes as a start, is a C7 failure (`prefix_changed`). `system` and `tools` never change in place: a new config pin is a new thread.
- **History prefix (cache reuse, not C7).** Each recorded turn request's bytes are a byte prefix of the next turn request's bytes, unless a `compacted`, `context_edited`, `settings_changed` or denying `before_input` decision lies between them. Compaction side requests are excluded. The render runner checks this as a separate property. It never stands in for C7.

## Semantic rules

Checked by readers and writers (`validate_next`) on top of the schema. This is the complete list of non-exportable rules: a hand-written check that isn't in this table is a CI error (AGENTS.md). Every rule has at least one conformance case.

| # | Rule | Error | Case |
|---|---|---|---|
| 1 | `seq` is contiguous along the resolved chain | `seq_mismatch` | `seq-gap-rejected` |
| 2 | `prev_hash` chains within each segment, and `fork.at_hash` matches the parent's line at `at_seq` | `prev_hash_mismatch` | `chain-middle-edit-detected`, `fork-at-hash-mismatch` |
| 3 | The head checkpoint equals the last line's seq and hash | `head_mismatch` | `head-checkpoint-suffix-removed` |
| 4 | Every event's `branch_id` equals its segment header's `branch_id` | `invalid_transition` | `event-branch-id-mismatch` |
| 5 | An unknown critical `(type, type_version)` refuses the log | `unsupported_critical_event` | `unknown-critical-event-refuses` |
| 6 | `epoch` is non-decreasing | `invalid_transition` | `epoch-decrease-rejected` |
| 7 | `tool_result` needs a pending `tool_call` with that `call_id`. `tool_result_late` needs an earlier `tool_result{origin: deferred}` for it | `invalid_transition` | `tool-result-without-call`, `late-result-without-placeholder` |
| 8 | `effect_begin` needs `permission_decision: allow` or a consumed matching approval, and no `cancel_requested` barrier after the call. `read_only` calls write no effect events | `invalid_transition` | `effect-begin-without-permission` |
| 9 | `call_id`, `channel_delivery.item_key`, `schedule_fired.occurrence_id` and approval consumption per `challenge_id` are each unique along the resolved chain (ancestor segments through their fork points plus the branch), as in rule 28 | `invalid_transition` | `duplicate-call-id` |
| 10 | `compacted`: both edges are step boundaries (no pending tool call and no model attempt awaiting a response just before `from_seq` and at `to_seq`), so a tool pair is never split; ranges never partly overlap; when `summary_request_event_id` is set, `summary_ref` is the UTF-8 text of that compaction request's response | `invalid_transition` | `compaction-splits-tool-pair`, `compaction-summary-mismatch-rejected` |
| 11 | `cancelled` is never written while an effect is `begun` or `unknown` | `invalid_transition` | `cancelled-with-unsettled-effect` |
| 12 | `user_input` opens a turn only when none is open; input during a turn is `steer`. A turn ends at `turn_completed` | `invalid_transition` | `user-input-inside-open-turn` |
| 13 | `approval_granted` / `approval_denied` match the open challenge's `call_id` and `args_hash` | `approval_mismatch` | `approval-args-mismatch` |
| 14 | Every `declared_prefix` equals the line 0 derived from the pinned settings of its authorized settings epoch (C7 per epoch). An epoch starts only at a `settings_changed` whose actor is authorized (schema: `reason: user` needs a principal, as actor `user` or `host`; automatic reasons need actor `host` or `recovery`. The host still authorizes the principal at write time: the schema only proves one is named), and a mismatch never resets the baseline | `prefix_changed` | `prefix-declared-changed-fails`, `prefix-stable-across-turns`, `settings-change-new-prefix-epoch`, `prefix-changed-mid-epoch-rejected` |
| 15 | `request_ref` bytes equal Render v1 of the events before the request (plus the instruction line for a compaction request) | `request_hash_mismatch` | `render-req-hash-mismatch`, `compaction-summarizer-recorded` |
| 16 | A `fork{reason: snapshot}` is at an eligible snapshot: quiescent and not expired | `no_snapshot_boundary`, `snapshot_expired` | `fork-not-at-snapshot-error`, `fork-snapshot-expired` |
| 17 | `tools_changed.tools_hash` is the SHA-256 of the RFC 8785 bytes of its `tools` | `invalid_transition` | `tools-changed-hash-mismatch` |
| 18 | `settings_changed` is not written while a model attempt awaits its response, and its model is listed in `policy.models` when a policy is present | `invalid_transition` | `settings-change-during-attempt-rejected` |
| 19 | `context_edited` names only calls with a recorded result; a redaction's `part` is a text part and its spans lie inside it on character boundaries | `invalid_transition` | `context-edit-unknown-call-rejected` |
| 20 | `output_validated.schema_sha256` equals `policy.output.schema_sha256`, and an `accepted` value validates against `policy.output.schema` | `invalid_transition` | `output-validated-schema-mismatch-rejected` |
| 21 | After `budget_exceeded`, no `model_request` until a new `user_input` | `invalid_transition` | `model-request-after-budget-exceeded-rejected` |
| 22 | `agent_finished` appears exactly once per `agent_spawned` child | `invalid_transition` | `agent-finished-twice-rejected` |
| 23 | `team_task_claimed` needs an existing open, unclaimed task whose blockers are completed; `team_task_updated` needs a claimed task; `team_message.message_id` is unique | `invalid_transition` | `team-task-claim-blocked-rejected` |
| 24 | `todos_updated` item ids are unique | `invalid_transition` | `todos-duplicate-id-rejected` |
| 25 | `tool_result{origin: answered}` needs an open `parked{address: {kind: input, id: <call_id>}}` | `invalid_transition` | `answer-without-open-question-rejected` |
| 26 | After `handoff`, the thread takes no `user_input`, `steer` or `model_request` | `invalid_transition` | `handoff-then-input-rejected` |
| 27 | `mode_changed.from` is the current mode; `to: bypass` needs `policy.permissions.allow_bypass` | `invalid_transition` | `mode-change-bypass-not-allowed` |
| 28 | `event_id` is unique along the resolved chain, checked on append and on import. SQLite's `(branch_id, event_id)` index is physical only; the writer and importer also check every ancestor segment through its fork point | `invalid_transition` | `event-id-duplicate-across-fork-rejected` |


**Structural errors** (import stage 2, checked on each line before `seq`, `prev_hash` and `validate_next`):

| Structure | Error | Case |
|---|---|---|
| The first line is not a header (an event before any header) | `invalid_transition` at that line | `event-before-header-rejected` |
| A child segment header is not immediately followed by its `fork` event | `invalid_transition` at the event after the header | `child-header-without-fork-rejected` |
| A `fork` event anywhere other than right after a child header | `invalid_transition` | `fork-event-mid-segment-rejected` |
| A fork link that doesn't match the parent's line at `at_seq` | `prev_hash_mismatch` at the fork | `fork-at-hash-mismatch` |
| Importing a branch whose rows already exist: identical bytes are an idempotent no-op; any different byte is `seq_conflict` at the first differing seq | `seq_conflict` | planned: `import-existing-branch-conflict` |

`invalid_line` means the line fails on its own (import stage 1). A line that is valid alone but misplaced is `invalid_transition`.

A line that fails its schema is `invalid_line`. Examples: a `tool_use` part inside `user_input`; a `settings_changed` by the model; a `settings_changed` with an automatic reason (`fallback`, `escalation`, `revert`) whose actor is not `host` or `recovery`; a `budget_exceeded` with scope `ancestor` and no `owner_thread_id` (`user-input-tool-use-part-rejected`, `settings-change-by-model-rejected`, `settings-change-auto-reason-by-user-rejected`, `budget-exceeded-ancestor-without-owner-rejected`). These are schema conditionals (`if`/`then`), not semantic rules.

## Versioning policy

- **`type_version` per event type.** Any change to a type's `data` bumps its version.
  - Old versions stay readable forever, through pure upcasters (`v → v+1`) implemented identically in both languages. Each upcaster is golden-tested against shared fixtures.
  - Writers always emit the latest version. Logs are never rewritten, and `seq`, `event_id` and `prev_hash` never change. The chain is verified over stored bytes before upcasting.
- **`format_version` in the header** covers only framing changes that a type version can't express. Bumping it needs a new ADR and a migration plan. It is not expected before 1.0.
- **Pre-freeze additions.** Until the v0.1 release freezes wire v1, additive changes (new event types, new optional fields, new enum values, new content part variants) land in `type_version` 1 in place. Every earlier fixture stays valid. ADRs 0019-0024 were added this way. After the freeze, every `data` change bumps its `type_version`.
- **This file is additive.** New types and new type versions are added to `events.v1.schema.json` in place, and the golden pins catch any removal or rename. `v2` of the file exists only alongside `format_version: 2`.
- **Reserved in v1** so that adding the loop mode later doesn't change the log format: `tool_result{origin: deferred}` plus `tool_result_late` (non-blocking tool calls, never rewriting history), and the control events `heartbeat`, `steer` and `stop_when_idle`. A hard stop is `cancel_requested`, so there is one stop barrier, not two.
- **One version table for both languages.** TS and Python release in lockstep from this file. A reader that meets a newer `(type, type_version)` follows the critical rule. It never guesses.
