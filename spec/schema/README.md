# spec/schema

The wire contract for the threads log. Both implementations read and write exactly these bytes.

| File | What it is |
|---|---|
| `events.v1.schema.json` | JSON Schema (draft 2020-12) for one canonical line of a branch: the `Header`, an event (envelope plus per-type `data`), or the `Head` checkpoint |
| `host-api/` | The host HTTP API: `openapi.json` (routes) and `host-api.v1.schema.json` (bodies and projections, `$id` `urn:threads:schema:host-api:v1`) |
| `api.schema.json` | The shape of `../api.json`, the public API contract |
| `store.sql` | The normative SQLite DDL of the log store, versioned by `PRAGMA user_version`. Embedded in both implementations by `../tools/gen_store_sql.py` (`--check` in CI) |

**Every `api.json` entry is explained.** Every public function, type, method, parameter, option, field and property, and every field of an inline object and parameter of a callback at any depth, has an explanation. Inputs (parameters, options, inline object fields, callback parameters) always have their own `doc`, and an optional one shows its `default` or says what omitting it does. A field or property may instead take the first sentence of the type it references, unless its type is primitive, `JsonValue` or a union. `../tools/check_api.py` enforces it with the rule in `../tools/api_docs.py`, which the docs generator uses for its rows.

## Ownership

One schema, three forms, with a single author.

1. **Authored in Zod 4** in `typescript/packages/core/src/log/`, descriptions included. The TS types are `z.infer` of it.
2. **Exported** with `z.toJSONSchema` into `spec/schema/events.v<N>.schema.json` by `bun run schema:export` (in `typescript/`), and committed. Keys are in RFC 8785 order, so the bytes are stable.
3. **Generated** into Pydantic v2 models in `python/` from the committed JSON Schema, with pinned generator settings that never emit `Any`. Arbitrary JSON (tool input, model params, MCP payloads) is the recursive `JsonValue` type, not `Any`.

CI regenerates both steps and fails on any diff (`bun run schema:check`, `tools/regen_models.py --check`). Nobody edits the exported file or the generated Python by hand; change the Zod source and re-export.

Cross-field rules that Zod has no form for (`if`/`then`/`else`, `not`, `oneOf` over `required`, `minProperties`) are written once, as JSON Schema data, next to the Zod shape they narrow (`withRule` in `src/log/rules.ts`). The export copies them verbatim and both languages enforce the same data at parse time, so they are part of the schema, not hand-written checks.

Rules that JSON Schema can't express at all are allowed only when they are listed under [Semantic rules](#semantic-rules) with a conformance case. Both languages implement them by hand.

## The API surface gate

Both packages are checked against `../api.json` in CI. TypeScript: `tools/gen_api_surface.py` emits type-level assertions that `tsc` compiles (`bun run typecheck`). Python: `tools/check_surface.py --lang py` imports the package and inspects it.

**What it checks**, in each language:

- every function and method exists and is callable;
- every option exists, with its required flag, across all overloads;
- every type is exported from its declared `package` entry (a `placement` gap names the other public entry that exports it instead, `at`, and the gate checks it is there);
- every field of a data type exists and can be omitted exactly when the contract marks it optional: in TypeScript a `?` property; in Python the type's constructor inputs (a parameter with a default is optional) or its TypedDict keys (a top-level `Required`/`NotRequired` counts even in a postponed annotation);
- every property of a handle or protocol exists; TypeScript also checks its `?` (a Python protocol has no optional attributes);
- an `optional` method is an optional property of the base type in TypeScript, and in Python a `@runtime_checkable` protocol named by `capability` that declares it, never a member of the base protocol.

**What it doesn't check:**

- signatures: parameter and return types, positional parameters and option value types are left to each language's type checker and the tests;
- that a Python constructor stores its inputs as attributes;
- **overload corner cases beyond the window** (an accepted scope limit): TypeScript's type system can't count overloads, so the gate reads a window of a function's last eight signatures (`this`, parameters and return type) and fails unless it can see all of them. Overloads it can't tell from the compiler's padding can hide an earlier one: four consecutive identical signatures followed by more, or any other variant past the eighth overload that differs from its neighbours only in ways the window doesn't compare. A contracted function with more than a handful of overloads is reviewed by hand.

**The gaps registry.** Anything either language lacks is listed in `../api-surface-gaps.json`, the one registry of what isn't built (the docs reference reads it too), one gap per member and language (a Python optional method may have two: its base protocol and its capability protocol), each with its owning lane. It only shrinks: a listed gap that is fixed fails until its entry is deleted, and a PR may add an entry only for a member its own contract change introduces (compared against the base commit's `api.json`). It must be empty at the release gate (`--release`).

**Test evidence.** `../api-coverage.json` names, for every function, required method and required option in each language that has it, a test that must pass in the same CI run's JUnit report. That proves a named, reviewed test exists and passed; that it exercises the member is a reviewed claim, not something CI checks.

## One contract for storage and interchange

SQLite is the storage engine. JSONL is the interchange, export and conformance format. They hold **the same bytes**:

- Each branch stores its header line and its own event lines, byte for byte as written (`branches.header_line`, `events.line`). The head checkpoint `(seq, hash)` is stored in `branches` and updated in the same transaction as every append.
- `threads export <branch>` writes those stored lines, one per line with `\n`, followed by the `Head` line. Import verifies every line, the chains and the head, then stores the same bytes.
- The hash chain is over those bytes. A fixture that verifies as JSONL verifies in SQLite, and the reverse.

## Snapshot manifest

A snapshot's `manifest_hash` is the RFC 8785 hash of its manifest: a JSON array of `{path, mode, size, sha256}`, one entry per captured file. `path` is relative to `/workspace` (no leading `/`). Entries are ordered by `path` in **UTF-16 code-unit order**, the same order JCS uses for object keys; code-point order differs above the BMP (U+1F600 sorts before U+E000). JCS canonicalizes keys, not array order, so every manifest builder must sort this way. Shared vector: `spec/conformance/vectors/manifest-order.json`.

**Sandbox image.** A remote sandbox image provides `/bin/sh`, `find`, `stat -c` (`%a`, `%s`), `sha256sum`, `od`, `cut`, `tr`, `mkdir`, `rm`, `dirname`, `env` (GNU coreutils, proven by the Linux CI job; BusyBox is not gated, and BSD/macOS `stat` is not supported). `lsp` also needs `python3` on the image `PATH`; a bare language-server command is looked up in `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`, since the driver runs with no `PATH`; an absolute command is run as given. The server runs with exactly that `PATH` (and no host env), so a `#!/usr/bin/env node` launcher needs its interpreter there too. A command's argv[0] is resolved on the provider `PATH`; the command then runs with exactly the tool env.

## Wire rules

1. **snake_case on the wire,** in both languages. Language APIs may re-case accessors (`forkPoints` ↔ `fork_points`), but stored and hashed bytes never change.
2. **Lines.** Each line is one JSON object in **RFC 8785 (JCS) canonical form**. The maximum line is 1 MiB; anything larger goes in an artifact.
   - Writers emit JCS, so a Python writer and a TS writer produce identical bytes for the same event.
   - **Admission:** a reader admits a line only if its bytes equal the RFC 8785 serialization of its parsed value. Anything else (`1e3`, `1000.0`, `-0`, keys out of order, whitespace, a non-minimal or uppercase `\u` escape, `\/`) is `invalid_line`. This runs after strict JSON parsing and before format admission (the header and head rule below) and the schema (`line-noncanonical-*`).
   - **Nesting:** a line nests at most **64** levels of arrays and objects (the line object is level 1). A deeper line is `invalid_line`, checked while parsing so no stage depends on stack depth (`line-nesting-too-deep`).
   - Readers hash the **raw bytes as stored** and never re-serialize to compute or verify a hash. The admission check compares bytes; it never replaces them.
3. **Hashes.** All are lowercase hex SHA-256 (names the two domains).

   | Field | Input bytes | Domain |
   |---|---|---|
   | `prev_hash` | the previous line in the same segment, without `\n`; the first event of a segment hashes its header | raw |
   | `Head.hash` | the branch's last line (its header when `seq` is 0) | raw |
   | `fork.at_hash` | the parent's line at `at_seq` | raw |
   | `request_ref.sha256` (= `req_hash`), `declared_prefix.sha256` | [Render v1](#render-v1) bytes | raw |
   | `ArtifactRef.sha256` | the artifact's bytes, full 64 hex digits | raw |
   | `config_hash`, `args_hash`, `manifest_hash`, `tools_changed.tools_hash` | RFC 8785 bytes of the structured value | canonical |

4. **Numbers.** Envelope fields and framework-authored integer fields are integers in `0 … 2^53−1`. Floats appear only in `JsonValue` positions (tool `input`, `model_params`, adapter `settings`), spelled per the spec. Fractions in policy are integer permille; prices are integer nano-units per token, of `policy.currency` (`agent()` pins `"USD"` whenever a pinned model declares a price, so its prices are nano-USD). A thread pinned before that rule keeps its pin: it reads and reduces unchanged, with a null `cost`, and continuing it through `agent()` is refused like any other config change (`invalid_config`), since its `config_hash` differs from the fresh pin's. Token counts are an integer or `null` (unknown, never zero).
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
    - **Recovering an open turn**. After the in-doubt items are settled, a turn with no `turn_completed` and nothing pending (no unsettled effect or call, no model request awaiting its response, nothing parked) is closed with `turn_completed{reason: interrupted}` (actor `recovery`), unless none of its input has been sent yet: no `model_request` follows the turn's last `user_input` or `steer`. Then recovery appends nothing and the run continues the turn normally. This holds for every recovery, not only after a torn import (`recover-open-turn-answered-interrupted`, `recover-open-turn-unsent-continues`, `torn-tail-wrong-head-unterminated`).
12. **Branches, segments and fork.**
    - A root branch is one segment: its header, then events from `seq` 1.
    - A child branch starts with its own header, then a `fork` event at `seq = at_seq + 1`. The fork's `prev_hash` is the hash of the child's header, and `fork.at_hash` is the hash of the parent's line at `at_seq`. The **resolved chain** is the parent's resolved chain through `at_seq`, then the child's own events.
    - Parent rows are **referenced, never copied or rewritten**. SQLite stores only the child's own lines. An export of a child writes each ancestor segment (its header and its lines through the fork point, byte-identical), then the child segment, then the child's `Head`.
    - A parent branch can't be deleted while a child references it (`branch_has_children`). Deleting a thread deletes all its branches together.
    - `fork.reason` is `snapshot` for a user fork at an eligible snapshot event or `repair` for operator repair of a corrupt parent.
    - A `snapshot` fork records `knowledge_policy`: `pinned` (the API default) searches knowledge as of the fork snapshot's `knowledge_revision`; `current` searches the live corpus. A `repair` fork has neither a sandbox nor a knowledge policy.
    - **A fork is never resumed.** If the host dies between creating the child (branch state `forking`, ledger rows written) and appending the child's `fork` event, recovery marks the child `fork_failed` and moves every ledger row the fork wrote to `releasing`, then releases them. No child is listed, and the parent is untouched (`fork-crash-no-orphan`).
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
  - `system` is `thread_started.instructions`, pinned by the runtime as these parts in order, each omitted when empty, joined by `\n\n`: the agent's base instructions; each extension's instructions in declaration order; `Subagents you can start with spawn_agent: <names>.` when the agent lists subagents; `Agents you can hand the conversation to: <names>.` when it lists handoff targets. `<names>` are the agent names in declaration order joined by `, ` (`render-agents-listed-in-system`).
  - The framework tools are catalog entries, pinned `read_only` and sorted by name together with the built-ins: `todo_write` always; `spawn_agent`, `send_message` and `team_task_claim`, `team_task_create`, `team_task_update` when the agent lists subagents; `handoff` when it lists handoff targets (the names are also `policy.handoffs`). A spawned child pins `send_message` and the `team_task_*` tools as a team member, and only tools its parent pinned (`final_output` excepted).
  - `model`, `params` (`model_params`) and `adapter` come from the latest `thread_started` or `settings_changed` before the request.
  - `adapter` is the model adapter's name, version and every provider-conversion setting, including declared hosted tools.
  - `tools` is `[{name, description, input_schema}]` in declared order. A spec with `defer_loading: true` renders as `{name, description, deferred: true}`. `effect_class`, `dedup_window_ms`, `output_schema` and `ends_turn` aren't model-visible and are omitted.
- **Framing escape.** Every framing wrapper (`<reference>`, `<context>`, `<heartbeat>`, and the summary reference) escapes each attribute value and its whole body text with `esc`: replace `&` with `&amp;` first, then `<` with `&lt;`, `>` with `&gt;`, `"` with `&quot;` and `'` with `&#39;`. Only the wrapper's own tags are raw, so stored text (a memory id, a memory body, a summary, a call id) can never close a wrapper or open another one. The escape applies to the rendered text only: artifacts and event payloads keep their original bytes (`render-reference-framing-escaped`).
- **Then one line per model-visible event, in log order:**

  | Event | Line |
  |---|---|
  | `user_input`, `steer` | `{"role":"user","content":[{"type":"text","text":<text>}]}`, or `{"role":"user","content":<content>}` when the event has `content` (ordered input parts, as recorded). **Nothing** if a later `hook_decision{hook: before_input, decision: deny \| failed, input_event_id}` before the request names it |
  | `injected`, trust `untrusted_reference` | A user line whose text is `<reference source="esc(S)" id="esc(ID)" untrusted="true">\nesc(TEXT)\n</reference>`. **Nothing** for `source: memory` recalled for another principal ([Memory in shared threads](#memory-in-shared-threads-adr-0015-invariant-6)) |
  | `injected`, trust `trusted_instruction` | A user line whose text is `<context source="esc(S)" id="esc(ID)">\nesc(TEXT)\n</context>` |
  | `heartbeat` | A user line whose text is `<heartbeat>\nrunning: esc(ID), esc(ID)\n</heartbeat>` |
  | `model_response`, `model_response_recovered` | `{"role":"assistant","content":<content>}`, parts as recorded. `reasoning` and `hosted_tool` parts of a response recorded before a `settings_changed{reasoning_carryover: omit_prior}` are omitted. If nothing is left, no line. **Nothing** when the response answers a `model_request{purpose: compaction}` |
  | `tools_changed` | `{"role":"tools","tools":[…]}`: the complete new set, rendered as in line 0 (deferred specs as stubs). Line 0 is unchanged |
  | `tool_result` | `{"role":"tool","call_id":…,"is_error":…,"content":<parts>}`. `<parts>` is `[{"type":"text","text":<preview>}]` when the event has no `content`, else `content` as recorded (the preview is then not rendered). The event's `ref` (the full output) is never rendered or read. A `context_edited` before the request changes it: `clear` makes `<parts>` exactly `[{"type":"text","text":"[tool result cleared: call_id=<id>; read it with read_tool_result]"}]`; `redact` gathers the spans (UTF-8 bytes of the original text) of text part `part` from every `context_edited` before the request, merges them into their disjoint union (overlapping or adjacent spans become one), and replaces each merged span with `[redacted]` (`render-redaction-spans-overlap`) |
  | `tool_result_late` | The same, plus `"late":true`. The earlier placeholder line stays |
  | host-originated call | A `tool_result` or `tool_result_late` whose `call_id` is no `tool_use` part of any `model_response` or `model_response_recovered` on the chain renders nothing: it answers a call the host issued itself (a `channel_send` reply, call_id `send_<source seq>_<op index>`), not one the model made, and the model's own final text is already in history. Its effect events are recorded as for any call (`render-host-channel-send-hidden`) |
  | `compacted` | The lines of events `from_seq..to_seq` are dropped (a compacted event inside a later range is dropped too). In place of the range's first event go: one user line, the summary artifact's text wrapped (escaped) as a reference with `source="summary"` and `id=<summary sha256>`, then the line of the **last** `tools_changed` inside the range, if any, so loaded and changed tools survive. The compacted event itself renders nothing. Line 0 is never compacted |

  All other events render nothing (`model_request`, `settings_changed`, `context_edited`, `context_preflight_blocked`, `hook_decision`, …).
- **Artifacts.** Parts carry artifact refs, not bytes. Before dispatch every artifact referenced by a rendered part (`citation` parts included) is read and verified. A read verifies both the ref's `sha256` and its `bytes` length; either mismatch is `artifact_corrupt` (`render-artifact-length-mismatch`). An event whose text is in an artifact (an `injected` `ref`, a `compacted` summary) renders the verified bytes decoded as UTF-8; bytes that aren't valid UTF-8 are `artifact_corrupt`, never decoded lossily (`render-text-artifact-not-utf8`). A missing or corrupt artifact is `artifact_missing` / `artifact_corrupt` at the seq of the event carrying the ref, never substituted, and the request isn't sent (`render-image-artifact-missing`).
- **Compaction side request** (`model_request{purpose: compaction}`): the normal render of the events before it, then one user line whose text is the fixed instruction `Summarize the conversation so far for your own continuation. Keep the user's goals and constraints, decisions made, files and identifiers touched, open tasks with their status, and the next step. Reply with the summary only.` followed, when `before_compact` hooks returned `guide` decisions with a `reason` since the previous `model_request`, by `\n\nAdditional instructions:\n` and those reasons joined by `\n`. A guide without a `reason` adds nothing. Its line 0 is the epoch's line 0, so C7 holds.
- **Provider wire bytes** are derived deterministically from these bytes and the verified artifacts by the adapter that line 0 names, using only the settings line 0 records. So every adapter-visible setting is inside the hashed bytes. An adapter that can't encode a rendered part fails before dispatch with `content_unsupported` or `continuation_unsupported`.
- **C7, the declared prefix, per settings epoch.** `model_request.declared_prefix` describes line 0 (bytes including its `\n`, and SHA-256). Within one settings epoch every recorded `declared_prefix` must be **equal** to every other and to that epoch's line 0 as re-rendered. A prefix that changes without a `settings_changed`, including one that grows while keeping its old bytes as a start, is a C7 failure (`prefix_changed`). `system` and `tools` never change in place: a new config pin is a new thread.
- **History prefix (cache reuse, not C7).** Each recorded turn request's bytes are a byte prefix of the next turn request's bytes, unless a `compacted`, `context_edited`, `settings_changed` or denying `before_input` decision lies between them, or an `injected{source: memory}` line is hidden by a change of input principal ([Memory in shared threads](#memory-in-shared-threads-adr-0015-invariant-6)). Compaction side requests are excluded. The render runner checks this as a separate property. It never stands in for C7.

## Team tools

A team tool call changes the lead's log through the lead's writer. `<member>` is the calling agent's name (the lead's own name for the lead's calls). Every act is keyed by the member's call, so a call re-dispatched after a restart answers the same and appends nothing (`team-tool-replay-appends-nothing`). The call's `tool_result.preview` is exactly:

| Tool | Appends | Preview | Repeated call |
|---|---|---|---|
| `team_task_create{subject, description?, blocked_by?}` | `team_task_created{task_id: "<member>/<call_id>"}`; an unknown blocker appends nothing: `no task <id>` (error) | the task id | the task exists: its id, nothing appended |
| `team_task_claim{task_id}` | `team_task_claimed{task_id, member}` when rule 23 allows; else nothing: `can't claim: <reason>` (error) | `claimed <task_id>` | already claimed by this member: `claimed <task_id>` |
| `team_task_update{task_id, status}` | `team_task_updated` when this member holds the claim; else `<task_id> is not claimed by <member>` (error) | `<task_id> <status>` | the task already has that status: `<task_id> <status>` |
| `send_message{to, text}` | `team_message{message_id: "<member>/<call_id>", from: <member>, to, text}` | `sent` | the message_id exists: `sent` |
| `spawn_agent{agent, ...}` while this lead already has an unfinished member of that name | nothing (no `agent_spawned`, no child thread) | `member_active: <agent> is still running` (error) | the same refusal, nothing appended |

**A member is its agent name, so names are unique within a team.** A lead's children are its team. `spawn_agent` for an agent name whose earlier child of this lead has `agent_spawned` and no `agent_finished` (foreground or background) is refused with the `member_active` result above. There is never a second concurrent instance of a name. Once that child has finished, the name can be spawned again, and it is the same member (its tasks and messages keep `<member>/<call_id>` ids).

A member receives each `team_message` addressed to it or to `*` (and not from itself) once, before its next turn request, as `injected{source: agent, trust: untrusted_reference, origin: {id: message_id}, text: "<from>: <text>"}`; one already injected with that `origin.id` is never injected again.

## Parallel tool calls

An app tool declared `concurrent: true` (only with `effect_class: read_only` and without `ends_turn`) may run at the same time as its neighbours. Before dispatching, the loop takes the pending calls in call order and forms a **group**: the longest run, from the first pending call, of calls whose bound tool is declared concurrent, whose pinned `effect_class` is `read_only`, and whose recorded `permission_decision` is `allow` (an approved `ask` still runs alone). Framework, built-in and MCP tools are never in a group. Every other call runs alone, after every earlier result is recorded and before any later call starts, so an effect never overlaps anything and its `effect_begin` stays durable and fenced before dispatch (invariant 3). The rule is pinned by `conformance/vectors/tool-groups.json`.

- **Recorded in call order.** The lease is checked immediately before each body starts. Results (and their `injected` events and `after_tool` decisions) are appended in call order, never in finish order. At most 8 calls are started and unrecorded at once: call *i + 8* starts only after result *i* is recorded. TypeScript records authorization with the calls, so a group's log equals a sequential run's; Python authorizes lazily, so a group's `before_tool`/`permission_decision` events all come before its first result.
- **No effect events.** A group holds only `read_only` calls, which write none (rule 8), so there are no new events or rules.
- **Stopping.** A `cancel_requested` noticed during a group lets the started calls finish and be recorded; queued calls never start and close `not_executed` at the barrier. A lost lease records nothing more; started bodies are aborted (TypeScript signal, Python task cancel) and awaited. After a crash, a `read_only` call without a result is simply run again, in call order.
- **Pinned by `config_hash`.** The sorted names of the concurrent tools are hashed as `concurrent_tools` (omitted when empty); they are not in `ToolSpec` or Render line 0.

## Subagent cancellation and parking

- **Tree-wide cancel.** `Thread.cancel` appends the thread's own `cancel_requested` (the barrier), then, for every child its log has an `agent_spawned` for and no `agent_finished`, appends `cancel_requested{scope: tree, reason: "ancestor cancelled"}` (actor `host`, principal the canceller) to that child's thread, recursively through the child's children. A child whose thread doesn't exist yet gets nothing: it is never started.
- **Nothing new after a barrier.** A run that finds `cancel_requested` in its open turn dispatches nothing new: calls that never began close `not_executed` (rule 8), and an in-doubt effect settles through recovery or parks (rule 11). A pending `spawn_agent` call whose child hasn't finished is not closed `not_executed`: the parent runs the child to its end (a child with no thread is recorded `cancelled` without being created; a child without a barrier in its open turn gets `cancel_requested{scope: tree}` first), records the child's one `agent_finished` and the call's result, and only then its own `cancelled` and `turn_completed{cancelled}` (`cancel-accepted-then-stopped`). A background child is cancelled the same way; its result arrives as `tool_result_late`.
- **A tree cancel is final.** Once a thread's open turn has a `cancel_requested` barrier, nothing gives that thread or any descendant new work. No hook (`subagent_stop` `continue`, `on_stop` and `before_input` included) appends input to a cancelled child or runs it again, and no `model_request` or `effect_begin` is appended anywhere in the subtree after its barrier. A hook decision that would continue is recorded and has no effect; the child's `agent_finished` is `cancelled` (behaviour test in both languages).
- **A child that parks parks its parent.** A child run that ends parked records no `agent_finished`. The parent appends `parked{address: {kind: child, id: <child_thread_id>}, reason: <the child's last park reason>}` once, the spawn call stays pending, and the parent's run ends `parked`, never `failed` (`child-parks-parent`). Each time the parent runs again it first runs every child it is parked on; for one that no longer ends parked it appends `resumed{address, cause_event_id: <that child's agent_spawned event_id>}` and continues: the call's re-dispatch runs the child again (which appends nothing new) and records its `agent_finished`. A host resumes the root thread after any control on a descendant. Under a cancel, a parked child parks the parent the same way, so `cancelled` never precedes a descendant's unsettled effect.

## Channel replies

A channel thread's outbound ops are derived from its log, never from how its run started (a channel item, the host API, a control, a restart).

- **Sources:** the last turn response (`model_response` or `model_response_recovered`) of each turn that ended `turn_completed{end_turn}`, and each `approval_requested` whose challenge is open (not answered, not expired).
- **Calls:** op `i` of `adapter.render(source)` is the host call `channel_send` with call_id `send_<source seq>_<i>`. Its input is the op plus `address`, `installation_id` and `last_inbound_at`: the `time` of the last `channel_delivery` before the source.
- **When:** after every run of the thread, and for every channel thread once the host is ready (from its first tick, never inside `ready()`), the host issues each derived call the log lacks. A call begun and unsettled is reconciled through the effect path, never re-sent blindly; a call with a result, or whose effect is parked, is left alone. So a crash between `turn_completed` and the reply's `tool_call` loses no reply and never sends one twice.
- **Approvals:** a card carries only its challenge id. A `decision` item answers that challenge only from a principal with [approval authority](#approval-authority-adr-0011-items-6-and-7), in the conversation's own thread, and only once (`approval_duplicate` after). Free text is a message, never a decision.
- **Queued items are never lost.** An inbox item is consumed in the same transaction as the append that applies it. A retryable failure (`branch_busy`, the process stopping) leaves the item queued for the next tick. Only a final outcome consumes it: applied, or refused (`forbidden`, `approval_duplicate`, `approval_expired`, an unknown challenge) with nothing appended.
- **Order.** A conversation's messages are applied in inbox order. A decision or control never waits behind a message the branch can't take yet (the thread is parked, or its lease is held): decisions and controls are applied in inbox order among themselves, ahead of such messages, and the messages follow in their own order.
- **After a handoff** the conversation moves with it. When a channel thread's run ends in `handoff`, the conversation's route (`channel_threads`) moves to `handoff.to_thread_id` with one conditional update keyed on (tenant, source thread), and its row count is checked: 0 rows means another process moved it, and the route is re-read, never overwritten. Later items are appended to the target thread and run with the target agent (the handoff target the source agent names). Replies are derived from the target thread's log. The source thread takes no further input (rule 26).
- **Item keys come only from signed content.** An `item_key` never depends on an unsigned header, so a replayed body is the same inbox item. GitHub: `<lowercase hex SHA-256 of the verified raw body>#0`. `X-GitHub-Delivery` is not signed and is never part of the key.

## Approval authority

One rule for the host API, channel decisions and recovery.

- **Whose set.** A challenge or parked effect on any thread is decided under the approver set of the **root run**: the agent that started the root of the thread's tree. A thread whose `thread_started.parent` is set (relation `subagent` or `handoff`) follows it to that thread, up to a thread with no parent. Reaching a descendant's challenge through the descendant thread's own route uses the same root set; the route never widens it.
- **Configured.** `agent({approvers})` of the root agent: exactly those principals, compared by `(issuer, tenant, subject)`. The requester is an approver only if listed.
- **Default.** With no `approvers` configured, the only approver is the root run's **originating principal**: the `actor.principal` of the root thread's latest `user_input` (for a channel thread, the sender whose message started the run). A host that wants stricter control configures `approvers`.
- **What needs it.** `approve` and `deny`, and every decision that accepts duplicate risk: `resolveParked` with either resolution (`assume_not_done` re-dispatches a possibly sent effect; `assume_done` settles an unproven one) and any human retry of a parked effect. A principal without authority gets `forbidden`, and nothing is appended. The in-process `Thread` API has operator authority (in-process code is trusted): it records the principal it is given.
- **Recorded.** The accepting principal is the `actor.principal` (actor kind `approver`) of `approval_granted`, `approval_denied` and `effect_resolved{by: human}`.

## Handoff scope

A handoff target runs inside the originating tree's limits, whoever hands off.

- **Ceilings.** The target's decisions are capped by every ceiling of the run that made the handoff: the host ceiling and the `Agent.run` ceiling and, when a subagent hands off, the subagent's own policy and every ancestor's policy up the spawn chain (a root agent's handoff target is capped by the run's ceilings, not by the source agent's policy: `handoff-target-policy-capped`). A subagent's handoff target never runs under fewer ceilings than the subagent.
- **Budgets.** Every budget covering the handing-off thread (its thread and run budgets and each one it inherits) also covers the target thread, as an ancestor's; a refusal is `budget_exceeded{scope: ancestor, owner_thread_id}`. The target's model requests reserve against those budgets and its own.

## Budget enforcement

- **A limit needs a per-attempt bound.** Each limit is reserved per attempt at the attempt's bound: `max_output_tokens` needs the epoch's `max_tokens`, a positive integer (zero, a negative, a fraction or a boolean bounds nothing); `max_input_tokens` needs `input_bound_tokens` or `input_billing_bound: context_window`; `max_cost_nanos` needs both and a price; `max_model_requests` is always 1. Setup (`validate` and every run start) refuses a limit covering a thread whose model, or any of its fallbacks, has no bound for it, with `budget_unenforceable`, unless `on_unknown_usage: stop`. The runtime check is the backstop: an attempt with no bound for a covering limit (after a `settings_changed`, or under `on_unknown_usage: stop`) is refused as exceeding, with `budget_exceeded{observed: <settled amount>, observed_is_upper_bound: true}`. A limit is never skipped and never reserved at 0.
- **The ledger is a cache of the logs.** Before each reservation the writer brings its branch's ledger rows up to date from the log: every `model_request` on the branch has a reservation, settled to its disposition once it has one. A lost, wiped or freshly imported `budget_ledger` is rebuilt from the tree's logs, never reset (invariant 1).

## Memory in shared threads (invariant 6)

- **Scope.** `search_memory`, `save_memory` and `forget_memory` act in the memory scope of the **current input's principal**: the `actor.principal` of the latest `user_input` or `steer` before the call. In a thread several principals write to (a shared channel conversation), each input is answered from its own sender's memory.
- **Visibility.** A recalled memory is shown only to the principal it was recalled for. Render v1 renders nothing for an `injected{source: memory}` event in a request whose current input principal (the latest `user_input` or `steer` before the request) differs from the current input principal before the `injected` event (`render-memory-other-principal-hidden`).
- **Scope string.** `Scope.scope` of a memory call is `<e(issuer)>/<e(subject)>`, where `e` replaces `%` with `%25` and then `/` with `%2F`. So `("a/b", "c")` and `("a", "b/c")` never share a scope, and a principal with neither character keeps its existing scope.
- **Recall listing.** `search_memory`'s `tool_result.preview` is `<n> memories, shown below as untrusted references` (or `no memories found`). Provider-chosen ids and versions appear only in each reference's `id` (`memory-save-recall-untrusted`).
- **Knowledge citations keep their ids.** `search_knowledge` results cite each passage as `[doc:<id>@<version>#…]`, with the provider's document id, inside the untrusted wrapper. A citation must name its source, and a document id is neither a secret nor scoped to a principal. Memory ids stay out of the listing because a memory belongs to one principal.
- **Knowledge ingest key.** The `key` passed to `KnowledgeProvider.ingest` is the `record_id` of the host binding issued for `(tenant, agent, scope, <path>@<sha256>)`, never the bare `<path>@<sha256>`. A file one agent or tenant has added is still added in another scope, and a provider that dedups on its key never answers across scopes. The host's own "already added" record is keyed per scope in the same way.
- **Knowledge passages.** A document splits into passages at every `\n`, then any run of whitespace, then `\n`; each passage is trimmed of whitespace, and an empty one is dropped. Spans are UTF-8 byte offsets into the document. Whitespace is exactly ECMAScript's WhiteSpace and LineTerminator set (what `\s` and `trim` use in TS): U+0009-U+000D, U+0020, U+00A0, U+1680, U+2000-U+200A, U+2028, U+2029, U+202F, U+205F, U+3000 and U+FEFF. It is not Python's `str.isspace` (U+001C-U+001F and U+0085 aren't whitespace here).

## Questions and remembered rules

- **Who answers.** An `ask_user` question is answered only by the principal whose input opened the turn that asked it: the `actor.principal` of the turn's `user_input`. Anyone else gets `forbidden`, and nothing is appended. A question asks that user, and it is not an approval.
- **Multi-choice.** An answer given as a list is recorded as its items joined with `\n` in `tool_result.preview`. A newline can't be confused with a comma that is inside a choice.
- **Suggested rules.** `PendingApproval.suggested_rules` for a `bash` call whose `command` is non-blank are `bash(<command>)`, then `bash(<w1> <w2>:*)`, where `w1 w2` are the first two words of the command as split by POSIX shell rules (Python `shlex.split`; one word gives `bash(<w1>:*)`). A command that doesn't split (an unclosed quote) gives only the exact rule. Any other call gives the tool name alone. A grant's `remember_rule` must equal one of the challenge's `suggested_rules`, otherwise the answer is `invalid_request`. Both languages suggest and accept the same rules.

## Secret redaction (C5, invariant 4)

Every credential value the host resolves, from a Secret or an explicit option, is replaced by `[secret <label>]` in everything recorded: every string of every event's `data` as the writer appends it (tool results and their parts, injected text, model output, hook decisions and injections, the run's own input), and every artifact the host stores beside events (a spilled or committed result, a spilled exec output, a compaction summary). Streamed model deltas shown to a caller are redacted too. A revealed Secret's label is its name; an adapter credential's is `<factory>.<option>` (`anthropic.apiKey` in TS, `anthropic.api_key` in Python); for equal values the smallest label wins.

- **Where.** The writer redacts every string of the stored line's `actor` and `data`, JSON object keys included (a redacted key that meets another is numbered ` (2)`, ` (3)`, … so no entry is lost). The envelope's framework fields are not credential material.
- **Matching.** Scanning left to right, the longest registered value that starts at each position is replaced (ties by code point). Lengths and order are by Unicode code point, in both languages.
- **The marker never repeats a value.** The marker is `[secret <label>]`, unless a registered value appears in it (value `api`, label `fake.apiKey`); then `[secret]`, then `[redacted]`, then nothing: the first that holds no registered value.
- **Provider material fails closed.** Reasoning, redacted thinking and hosted tool items are stored byte-exact because they are sent back as recorded (they may be signed or encrypted), so they are never edited. A response whose provider material holds a registered value stores none of it: the attempt is `model_attempt_abandoned{provider_outcome: unknown, reason: provider_error}` and the turn ends `turn_completed{error, code: secret_in_provider_output}`, never retried.
- **Fetched pages.** `web_fetch` stores the cited page after redacting its bytes (UTF-8 values; a page in another encoding is matched byte for byte as UTF-8). Non-text bytes are never edited: a non-text page that holds a registered value is refused and not stored.
- **Streams.** A stream (exec output bytes, model deltas) holds back its unredacted tail while it could still grow into a registered value, and decides it when more arrives or at the end. So `abc` then `123` with `abc` and `abc123` registered records one `[secret …]` for `abc123`, and a value split inside a multi-byte character is replaced whole.

## Server-originated text (invariant 6)

Text a tool server or provider chose reaches the model only inside the untrusted wrapper (`<reference source=... untrusted="true">`, escaped as in Render v1), never as bare tool-result text. This covers an MCP result, an MCP JSON-RPC error (`error <code>: <message>`), a memory or knowledge provider's ids and versions, and a web page. An MCP JSON-RPC error is final (the server answered), except `-32001` (request timeout) and `-32000` (connection closed), which leave the outcome unknown. An HTTP transport status (408 included) is never read as a JSON-RPC code.

## SSRF guard

`web_fetch`, and forge calls to a configured `api_url`, connect only to public unicast addresses: every resolved address is checked, and the connection goes to a checked one. Shared vector: `spec/conformance/vectors/ssrf.json`. Both guards must give its expected answer for every entry.

- **IPv4** is public unless it is in `0/8`, `10/8`, `100.64/10`, `127/8`, `169.254/16`, `172.16/12`, `192.0.0/24`, `192.0.2/24`, `192.88.99/24`, `192.168/16`, `198.18/15`, `198.51.100/24`, `203.0.113/24` or `224/3` (multicast, reserved and broadcast).
- **IPv6** is public only inside `2000::/3` and outside `2001::/23` (Teredo and the IETF protocol assignments), `2001:db8::/32`, `2002::/16` (6to4) and `3fff::/20`. Two forms carry IPv4 and are judged as their IPv4 address: IPv4-mapped `::ffff:0:0/96` and NAT64 `64:ff9b::/96`. Everything else outside `2000::/3` is denied, including IPv4-compatible `::a.b.c.d`, local-use NAT64 `64:ff9b:1::/48`, `100::/64`, ULA, link-local, site-local and multicast. A zone id (`%eth0`) is ignored. An unparseable address is denied.
- `blocked_domains` and `allowed_domains` compare hostnames lowercased with one trailing dot removed, so `evil.com.` is `evil.com`.

## Git gateway: `open_pull_request` (invariant 3)

- **Lookup before create.** Before creating, and when recovery reconciles a begun call, the gateway lists the repository's pull requests for `(head, base)` in **every state**. If one exists, the newest is the result and nothing is created, and its state is in the preview (`pull request #<n> (<state>)`). A pull request is created only when none exists. So a create whose response was lost, followed by a close, never leads to a second pull request.
- A create refused with 422 means "already exists" only when the forge says a pull request for the head already exists; the lookup then names it. Any other 422 is an error result.

## Deleting a thread

Deleting a thread (`threads delete`, the host's thread deletion) also deletes every **subagent** thread spawned under it, recursively: each thread whose `thread_started.parent` has relation `subagent` and names a deleted thread. The same rules apply to each: same tenant only, a tombstone per thread, log rows and projections removed, live resources moved to `releasing`, and ledger rows released. **Handoff targets are independent threads** (relation `handoff`) and are not deleted, and neither are their own subagents.

## Semantic rules

Checked by readers and writers (`validate_next`) on top of the schema, except rules 14 and 15: they need artifacts, so import checks them in step 3, request verification (spec/conformance/README.md, `render`), and the adapter guard re-checks them before dispatch. This is the complete list of non-exportable rules: a hand-written check that isn't in this table is a CI error (AGENTS.md). Every rule has at least one conformance case.

| # | Rule | Error | Case |
|---|---|---|---|
| 1 | `seq` is contiguous along the resolved chain | `seq_mismatch` | `seq-gap-rejected` |
| 2 | `prev_hash` chains within each segment, and `fork.at_hash` matches the parent's line at `at_seq` | `prev_hash_mismatch` | `chain-middle-edit-detected`, `fork-at-hash-mismatch` |
| 3 | The head checkpoint equals the last line's seq and hash | `head_mismatch` | `head-checkpoint-suffix-removed` |
| 4 | Every event's `branch_id` equals its segment header's `branch_id`. Every header and event carries the resolved chain's `thread_id` (the first header's); a child header naming another thread fails at the child's first seq | `invalid_transition` | `event-branch-id-mismatch`, `fork-cross-thread-rejected` |
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


### Output schemas (rule 20)

Both readers check `policy.output.schema` with their own evaluator of one keyword set, defined here, never with a library's reading of JSON Schema. A writer records a candidate `accepted` only if its agent's validator **and** this check pass it; otherwise it is `rejected` and the model tries again. An output model whose schema uses any other keyword is refused at setup (`ConfigError invalid_config`, naming the keyword).

- **Annotations** (never constrain): `title`, `description`, `default`, `examples`, `$defs`, `$schema`, `$comment`.
- **Structure**: `type`, `properties`, `required`, `additionalProperties`, `items`, `minProperties`, `enum`, `const`, `allOf`, `anyOf`, `oneOf`, `not`, `if`/`then`/`else`; `$ref` is `#` (the whole schema) or `#/$defs/<name>` that exists, recursion included, but no chain of `$ref`s through `allOf`, `anyOf`, `oneOf`, `not`, `if`, `then` or `else` may come back to where it started without reaching into the value through `properties`, `additionalProperties` or `items` (`{"$ref": "#"}` is refused). A reader given such a schema in an imported log fails closed: the value doesn't satisfy it (`output-schema-ref-cycle-rejected`). A property is the object's own key: a name like `toString` is never found on a prototype (`output-schema-required-own-key-rejected`, `output-schema-additional-own-key-rejected`).
- **Equality** (`const`, `enum`): a boolean equals only the same boolean; numbers compare by value (`1` equals `1.0`); strings and `null` exactly; arrays item by item in order; objects key by key, in any key order (`vectors/json-equal.json`).
- **Numbers**: `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum` (number form); `multipleOf` (positive) is exact in decimal: each number is read as its shortest round-trip decimal `d × 10^e`, and `value` is a multiple when, scaled to the smaller exponent, the value's digits are divisible by the divisor's (`0.3` is a multiple of `0.1`; `0.35` is not).
- **Lengths**: `minLength`/`maxLength` count Unicode code points; `minItems`/`maxItems` count items.
- **`pattern`**: searched (not anchored) in a string, written in this portable subset of ECMA-262 and meaning the same in both readers (`vectors/pattern.json`). Anything else is unsupported: refused at setup, failed closed by a reader.
  - Characters other than `\ ^ $ . | ? * + ( ) [ ] { }` match themselves; `\` before one of those or `/` or `-` makes it literal; `\t \n \v \f \r` are those controls.
  - `\d` is `[0-9]`, `\w` is `[A-Za-z0-9_]`, `\s` is `[\t\n\v\f\r ]`, and `\D \W \S` their complements; `\b` is a position between a `\w` character and a non-`\w` character or an edge of the string, and `\B` any other position (so `^\B$` matches the empty string).
  - `.` is any code point but `\n`, `\r`, U+2028 and U+2029 (an emoji is one); `^` is the start and `$` the very end of the string (a trailing `\n` is not skipped).
  - Groups `( )` and `(?: )`, lookaheads `(?= )` and `(?! )` (never repeated), alternation `|`; quantifiers `* + ? {n} {n,} {n,m}` after something repeatable, each optionally lazy with `?`.
  - Classes `[ ]` and `[^ ]` are non-empty; members are single characters (`[` escaped), `\d \D \w \W \s` and the escapes above, and ranges between two single characters, either of which may be an escaped character (`[A-\]]`, `[\t-\r]`) but never a set like `\d`; `-` is a literal only first or last, and a range's end never starts another range.
  - Limits, the same in every engine: at most 1000 code points in the pattern, quantifier bounds up to 1000, groups nested at most 32 deep.
  - Unsupported, among others: named groups, lookbehind, back-references, inline flags, `\p`, `\u`, `\x`, `\c`, `\0`, `\S` inside a class, possessive quantifiers, unescaped `{`, `}` or `]`.
- **`format`** constrains strings only; any name but these is unsupported:

| Format | A string matches when |
|---|---|
| `date` | `YYYY-MM-DD` (ASCII digits), month 01–12, day 01 to the month's length (Gregorian leap years) |
| `time` | `HH:MM:SS`, optional `.` and fraction digits, then `Z`/`z` or `±HH:MM`; hour 00–23, minute and second 00–59 (no leap second), offset hour 00–23 and minute 00–59 |
| `date-time` | a `date`, `T` or `t`, then a `time` |
| `email` | one or more characters other than `@` and ASCII whitespace, `@`, then two or more dot-separated labels of characters other than `@`, `.` and ASCII whitespace |
| `uri` | a scheme (`A-Za-z` then `A-Za-z0-9+.-`), `:`, then characters other than ASCII whitespace |
| `uuid` | `8-4-4-4-12` hex digits, either case, any version |

Cases: `output-schema-constraints-accepted`, `output-schema-formats-accepted`, and one `output-schema-<keyword>-rejected` per keyword and format.

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

## Turn endings by stop_reason

What the loop does after a turn response with no `tool_use` part (a response with one records its calls first). Both implementations follow this table; the cases pin it.

| `stop_reason` | Loop step | Case |
|---|---|---|
| `end_turn`, `stop_sequence` | The turn completes (`turn_completed{end_turn}`, after structured output when pinned) | `recover-open-turn-unsent-continues` |
| `refusal` | The turn completes as a refusal outcome (the text is the answer) | |
| `tool_use` | The recorded calls dispatch | |
| `max_tokens` | The fixed continuation instruction, up to `context.max_output_continuations`, then `turn_completed{max_output}` | `max-output-continuation-bounded` |
| `pause_turn` | A new `model_request` with nothing added, so the paused content goes back as-is. When the turn's `pause_turn` responses exceed `context.max_pause_continuations` (absent: 3), `turn_completed{error}` | `pause-turn-continues-as-is`, `pause-turn-bounded` |
| `context_window_exceeded` | `turn_completed{context_exhausted}`, never a completed run | `context-window-exceeded-ends-turn` |
| `other` | A reason the adapter can't name: `turn_completed{error}`, never a completed run | |

**Capability pre-check.** Before appending a `model_request`, the loop checks the rendered request against the model's declared capabilities: an `image_ref`, `document_ref` or `audio_ref` in a user or tool line that `accepts` doesn't list, or a `reasoning` or `hosted_tool` part whose `provider` isn't the model's. On a mismatch no `model_request` is appended and the turn ends `turn_completed{reason: error, code: content_unsupported | continuation_unsupported}` (`content-unsupported-before-dispatch`, `continuation-unsupported-before-dispatch`). An adapter that still refuses at send time returns a typed non-retryable rejection (`content_unsupported` or `continuation_unsupported`), never an unknown outcome that would be re-sent.

**Transport fence refusal.** A send rejected with `transport_fence_unsupported` (a bridge model that has detected its sends bypass the fenced transport, spec/api.json) is recorded as `model_attempt_abandoned{provider_outcome: not_sent}` and ends the turn `turn_completed{reason: error, code: transport_fence_unsupported}`. It is never retried.

## Versioning policy

- **`type_version` per event type.** Any change to a type's `data` bumps its version.
  - Old versions stay readable forever, through pure upcasters (`v → v+1`) implemented identically in both languages. Each upcaster is golden-tested against shared fixtures.
  - Writers always emit the latest version. Logs are never rewritten, and `seq`, `event_id` and `prev_hash` never change. The chain is verified over stored bytes before upcasting.
- **`format_version` in the header** covers only framing changes that a type version can't express. Bumping it needs a new ADR and a migration plan. It is not expected before 1.0.
- **Pre-freeze additions.** Until the v0.1 release freezes wire v1, additive changes (new event types, new optional fields, new enum values, new content part variants) land in `type_version` 1 in place. Every earlier fixture stays valid. ADRs 0019-0024 were added this way. After the freeze, every `data` change bumps its `type_version`.
- **This file is additive.** New types and new type versions are added to `events.v1.schema.json` in place, and the golden pins catch any removal or rename. `v2` of the file exists only alongside `format_version: 2`.
- **Reserved in v1** so that adding the loop mode later doesn't change the log format: `tool_result{origin: deferred}` plus `tool_result_late` (non-blocking tool calls, never rewriting history), and the control events `heartbeat`, `steer` and `stop_when_idle`. A hard stop is `cancel_requested`, so there is one stop barrier, not two.
- **One version table for both languages.** TS and Python release in lockstep from this file. A reader that meets a newer `(type, type_version)` follows the critical rule. It never guesses.
