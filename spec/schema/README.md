# spec/schema

The wire contract for the threads log. Both implementations read and write exactly these bytes.

| File | What it is |
|---|---|
| `events.v1.schema.json` | JSON Schema (draft 2020-12) for one canonical line of a branch: the `Header`, an event (envelope plus per-type `data`), or the `Head` checkpoint |

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
   - Readers hash the **raw bytes as stored** and never re-serialize to verify anything.
3. **Hashes.** All are lowercase hex SHA-256.

   | Field | Input bytes | Domain |
   |---|---|---|
   | `prev_hash` | the previous line in the same segment, without `\n`; the first event of a segment hashes its header | raw |
   | `Head.hash` | the branch's last line (its header when `seq` is 0) | raw |
   | `fork.at_hash` | the parent's line at `at_seq` | raw |
   | `request_ref.sha256` (= `req_hash`), `declared_prefix.sha256` | [Render v1](#render-v1) bytes | raw |
   | `ArtifactRef.sha256` | the artifact's bytes, full 64 hex digits | raw |
   | `config_hash`, `args_hash`, `manifest_hash`, `tools_changed.tools_hash` | RFC 8785 bytes of the structured value | canonical |

4. **Numbers.** Envelope fields and framework-authored integer fields are integers in `0 … 2^53−1`. Floats appear only in `JsonValue` positions (tool `input`, `model_params`, adapter `settings`), spelled per .
5. **Identifiers.** `thread_id`, `branch_id` and `event_id` are lowercase UUIDv7 strings, branded per kind in both languages. `call_id` is opaque and may come from the provider.
6. **Pinned names.** Event `type` names, every enum literal and every `ErrorCode` are frozen by a golden test in both languages. Add only. Never update the pins in a rename PR.
7. **Header** (a branch's first line): `{format: "threads.log", format_version: 1, thread_id, branch_id, created_at, writer: {impl, version}}`. It is not an event: no `seq`, no `prev_hash`. Every branch, root or child, has its own header. `writer` pins the only implementation and major version that may append.
8. **Head checkpoint** (the last line of every export): `{format: "threads.head", format_version: 1, branch_id, seq, hash}`.
9. **Envelope** (every event):

   | Field | Rule |
   |---|---|
   | `seq` | Contiguous along the branch's resolved chain. The first event after a root header is 1 |
   | `event_id` | Unique in the thread. All cross-event references use it |
   | `thread_id`, `branch_id` | `branch_id` is the branch that wrote the event, which is always its segment's header `branch_id` |
   | `epoch` | The writer's lease epoch. Non-decreasing along the resolved chain |
   | `type`, `type_version` | Discriminant plus per-type schema version |
   | `time` | Epoch ms. Informational only, never used for ordering |
   | `actor` | `{kind, principal?}`. `principal` is required on `user_input`, `steer`, `channel_delivery`, `cancel_requested`, `stop_when_idle`, `approval_granted` and `approval_denied` |
   | `prev_hash` | See the hashes table |
   | `critical` | Always present. See below |
   | `data` | Strict per-`(type, type_version)` payload. Unknown keys are rejected |

10. **Critical vs ignorable.** Every event states `critical` explicitly. For known types the value is pinned in the schema.
    - A reader that doesn't know an event's `(type, type_version)` **refuses the whole log** with `unsupported_critical_event` if the event is critical. This means a newer writer, not corruption.
    - If the unknown event is not critical, the reader keeps it: `reduce` skips it, but the head seq and hash still advance.
    - New types default to `critical: true`. Use `false` only when skipping the event cannot change `reduce` or render output (`log_repaired`, `park_escalated`, `schedule_skipped`).
11. **Epoch fencing.** Epochs come from the branch lease. Acquiring a lease sets `epoch = max(lease epoch, max epoch in the resolved chain) + 1`, and every append by that holder, recovery included, uses it. A writer, gateway or adapter holding a lower epoch than the lease is stale and rejects with `stale_epoch` before dispatching anything. Fencing stops our own dispatch path. It never proves that an earlier dispatch did not happen.
12. **Branches, segments and fork.**
    - A root branch is one segment: its header, then events from `seq` 1.
    - A child branch starts with its own header, then a `fork` event at `seq = at_seq + 1`. The fork's `prev_hash` is the hash of the child's header, and `fork.at_hash` is the hash of the parent's line at `at_seq`. The **resolved chain** is the parent's resolved chain through `at_seq`, then the child's own events.
    - Parent rows are **referenced, never copied or rewritten**. SQLite stores only the child's own lines. An export of a child writes each ancestor segment (its header and its lines through the fork point, byte-identical), then the child segment, then the child's `Head`.
    - A parent branch can't be deleted while a child references it (`branch_has_children`). Deleting a thread deletes all its branches together.
    - `fork.reason` is `snapshot` for a user fork at an eligible snapshot event or `repair` for operator repair of a corrupt parent (item 14).
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

14. **Integrity scope.**
    - The chain detects any change to a line before the last one: the next line's `prev_hash` stops matching.
    - A change to the last line, or removal of a valid suffix, is detected only against the committed head checkpoint (`head_mismatch`). SQLite keeps the checkpoint in the same transaction as the rows. An export carries it as its last line.
    - Neither is a signature. Anyone who can rewrite both the lines and the checkpoint can forge a consistent log. v0.1 claims detection of corruption, bugs and truncated copies, not tamper-proofing.
    - **Corruption at `seq k`** refuses writable opens with `log_corrupt{at_seq}`. Readers get the valid prefix plus a flag. Nothing is truncated in place. `threads repair <branch>` is an explicit operator command: it creates a new child with `fork{reason: repair}` at `k − 1` and marks the corrupt branch read-only. The corrupt rows stay as evidence, so the log stays append-only.
    - **Torn tails exist only in JSONL** (an interrupted copy). Import serves the valid prefix, flags the head as unverified, keeps the dropped bytes as an artifact, and the imported branch records `log_repaired{truncated_bytes, at_offset, dropped_ref}`. An invalid line followed by a valid line is `log_corrupt`, not a torn tail.

## Render v1

The one normative representation of a model request. `req_hash` covers these bytes, and the adapter guard re-renders and re-hashes before every dispatch (`request_hash_mismatch` on any difference). It is JSONL: each line is JCS followed by `\n`.

- **Line 0 is the declared immutable prefix:** `{adapter, model, params, system, tools}`, taken from `thread_started`. `params` is `model_params` (every adapter-visible generation parameter). `adapter` is the model adapter's name, version and every provider-conversion setting. `system` is the pinned instruction text. `tools` is `[{name, description, input_schema}]` in declared order (`effect_class` and `dedup_window_ms` aren't model-visible and are omitted).
- **Then one line per model-visible event, in log order:**

  | Event | Line |
  |---|---|
  | `user_input`, `steer` | `{"role":"user","content":[{"type":"text","text":<text>}]}` |
  | `injected`, trust `untrusted_reference` | A user line whose text is `<reference source="S" id="ID" untrusted="true">\nTEXT\n</reference>` |
  | `injected`, trust `trusted_instruction` | A user line whose text is `<context source="S" id="ID">\nTEXT\n</context>` |
  | `heartbeat` | A user line whose text is `<heartbeat>\nrunning: ID, ID\n</heartbeat>` |
  | `model_response`, `model_response_recovered` | `{"role":"assistant","content":<content>}` |
  | `tools_changed` | `{"role":"tools","tools":[{name, description, input_schema}]}`: the complete new set. Line 0 is unchanged; the adapter sends the latest set as the provider's tool parameter, so this is the one event that knowingly ends provider cache reuse |
  | `tool_result` | `{"role":"tool","call_id":…,"is_error":…,"content":[{"type":"text","text":<preview>}]}` |
  | `tool_result_late` | The same, plus `"late":true`. The earlier placeholder line stays |
  | `compacted` | The lines for `from_seq..to_seq` are dropped. In their place goes one user line: the summary artifact's text, wrapped as a reference with `source="summary"` and `id=<summary sha256>`. Line 0 is never compacted |

  All other events render nothing. An event whose text is in an artifact renders the verified artifact bytes decoded as UTF-8. A missing or corrupt artifact is a typed error, never substituted text.
- **Provider wire bytes** are derived deterministically from these bytes by the adapter that line 0 names, using only the settings line 0 records. So every adapter-visible setting is inside the hashed bytes.
- **C7, the declared prefix.** `model_request.declared_prefix` describes line 0 (bytes including its `\n`, and SHA-256). On a branch, every recorded `declared_prefix` must be **equal** to every other and to line 0 as re-rendered. A prefix that grows while keeping its old bytes as a start is a C7 failure (`prefix_changed`). Config changes don't happen in place: a new config pin is a new thread.
- **History prefix (cache reuse, not C7).** Each recorded request's bytes are a byte prefix of the next request's bytes, unless a `compacted` event lies between them. The render runner checks this as a separate property. It never stands in for C7.

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
| 9 | `call_id`, `channel_delivery.item_key`, `schedule_fired.occurrence_id` and approval consumption per `challenge_id` are each unique within a branch | `invalid_transition` | `duplicate-call-id` |
| 10 | `compacted` covers whole completed turns only | `invalid_transition` | `compaction-splits-turn` |
| 11 | `cancelled` is never written while an effect is `begun` or `unknown` | `invalid_transition` | `cancelled-with-unsettled-effect` |
| 12 | `user_input` opens a turn only when none is open; input during a turn is `steer`. A turn ends at `turn_completed` | `invalid_transition` | `user-input-inside-open-turn` |
| 13 | `approval_granted` / `approval_denied` match the open challenge's `call_id` and `args_hash` | `approval_mismatch` | `approval-args-mismatch` |
| 14 | Every `declared_prefix` on a branch is equal (C7) | `prefix_changed` | `prefix-declared-changed-fails`, `prefix-stable-across-turns` |
| 15 | `request_ref` bytes equal Render v1 of the events before the request | `request_hash_mismatch` | `render-req-hash-mismatch` |
| 16 | A `fork{reason: snapshot}` is at an eligible snapshot: quiescent and not expired | `no_snapshot_boundary`, `snapshot_expired` | `fork-not-at-snapshot-error`, `fork-snapshot-expired` |
| 17 | `tools_changed.tools_hash` is the SHA-256 of the RFC 8785 bytes of its `tools` | `invalid_transition` | `tools-changed-hash-mismatch` |

## Versioning policy

- **`type_version` per event type.** Any change to a type's `data` bumps its version.
  - Old versions stay readable forever, through pure upcasters (`v → v+1`) implemented identically in both languages. Each upcaster is golden-tested against shared fixtures.
  - Writers always emit the latest version. Logs are never rewritten, and `seq`, `event_id` and `prev_hash` never change. The chain is verified over stored bytes before upcasting.
- **`format_version` in the header** covers only framing changes that a type version can't express. Bumping it needs a new ADR and a migration plan. It is not expected before 1.0.
- **This file is additive.** New types and new type versions are added to `events.v1.schema.json` in place, and the golden pins catch any removal or rename. `v2` of the file exists only alongside `format_version: 2`.
- **Reserved in v1** so that adding the loop mode later doesn't change the log format: `tool_result{origin: deferred}` plus `tool_result_late` (non-blocking tool calls, never rewriting history), and the control events `heartbeat`, `steer` and `stop_when_idle`. A hard stop is `cancel_requested`, so there is one stop barrier, not two.
- **One version table for both languages.** TS and Python release in lockstep from this file. A reader that meets a newer `(type, type_version)` follows the critical rule. It never guesses.
