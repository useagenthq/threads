# spec/schema/ui: web UI protocols

The host serves a run to web UIs in two stock protocols, with no adapter in the browser: the Vercel **AI SDK UI message stream** (the `useChat` hook) and **AG-UI** (CopilotKit, `@ag-ui/client`'s `HttpAgent`). Every frame that carries an SSE id is a pure function of committed log events, so a replay of the log gives the same bytes in both languages. This file is normative, like Render v1. Conformance: the `ui` case kind (`../../conformance/README.md`) and the vectors `ui-thread-ids.json`, `ui-inputs.json` and `ag-ui-fold.json`.

| File | What it is |
|---|---|
| `ai-sdk-ui.v1.schema.json` | The chunk schema of **`ai@7.0.113`** (`uiMessageChunkSchema`), as the SDK converts it to JSON Schema (draft 7). Exported by `typescript/packages/host/scripts/export-ui-schemas.ts` (a dev script: `ai` is a dev dependency); its one custom type, the `data-*` chunk type, is written as the pattern its check applies. A host test fails when the export differs |
| `ag-ui-1.0.schema.json` | AG-UI protocol `"1.0"` (`@ag-ui/core@1.0.0`), vendored byte for byte from `docs/spec/1.0/schema.json` of `github.com/ag-ui-protocol/ag-ui`, tag `release/2026-09-23`, commit `aeeb693efb4183548d8a30c09d4143ce6f68ec38`, sha256 `4b5c93226838a0e72d88e6c5df20633c686c49fcb75d9be2815c6cbf9e48e71a` (94,359 bytes). Both test suites check the hash |

Only these versions are tested. A version bump is its own change: re-export or re-vendor, regenerate the fixtures, review the diff. A weekly CI job (`.github/workflows/ui-drift.yml`) runs the AG-UI fold comparison against `@ag-ui/client@latest`; it opens an issue on a mismatch and never gates a merge.

## Routes

| Route | What it does |
|---|---|
| `POST /v1/ui/ai-sdk/{agent}` | Body `AiSdkChatRequest`. A last user message starts a run (or replays the one its id already started); a last assistant message records its approval decisions and `ask_user` answers and streams the resumed run. Response: SSE with `x-vercel-ai-ui-message-stream: v1`, ended by `data: [DONE]` |
| `GET /v1/ui/ai-sdk/{agent}/{chat_id}/stream` | `useChat({resume: true})`: the key's latest run from its start while it is running; **204** once it has ended or parked, or when the key has no thread |
| `POST /v1/ui/ag-ui/{agent}` | Body `AgUiRunInput`. Starts, retries or resumes a run (below). Response: SSE of AG-UI events |
| `GET /v1/threads/{thread_id}/runs/{run_id}/ui/{protocol}` | For custom clients: committed frames only (no live text), resumed by `Last-Event-ID: <seq>:<k>` or `?after=<seq>:<k>` (the header wins). `protocol` is `ai-sdk` or `ag-ui`; any other is `not_found` |

Failures before the stream starts are `{error: {code, message}}` with the host's status map: `unauthenticated` 401; `not_found` 404 (unknown agent, thread, run or protocol); `invalid_request` 400 (a body that fails its parse, a malformed `thread_id` or `run_id` in the path, or one of the refusals below); `invalid_cursor` 400; the control codes of approvals and answers (`forbidden`, `approval_expired`, `approval_duplicate`, `no_open_question`, `branch_busy`, …). Frames are canonical JSON (RFC 8785). An SSE message is `id: <seq>:<k>\n` (log-derived frames only) then `data: <frame>\n\n`.

## Chat key

The browser names the conversation (`useChat({id})`, AG-UI `threadId`). The key is 1 to 128 characters of `[A-Za-z0-9_.-]` (anything else is `invalid_request`) and is **never recorded**. The thread is derived:

`thread_id = UUIDv8( sha256( lp("threads-ui-v1") ‖ lp(principalKey(principal)) ‖ lp(agent) ‖ lp(key) )[0..16] )`

- `lp(s)` is the 4-byte big-endian length of the UTF-8 bytes of `s`, then those bytes.
- `principalKey` is the escaped `issuer/tenant/subject` of `../README.md`; `agent` is the route's agent key.
- The first 16 bytes of the digest, with the version nibble (byte 6, high nibble) set to `8` and the variant bits (byte 8, top two bits) set to `10`, formatted as a lowercase UUID.

So the same key from two principals, two agents or two tenants names two threads. A UI thread is reachable through the UI routes only by the principal who created it; anyone else uses the thread-id routes, where approval authority is unchanged. A missing thread is created with that id on its first run.

## Bodies

Each body is parsed by `host-api.v1.schema.json` (`AiSdkChatRequest`, `AgUiRunInput`): only the fields threads reads, every other field ignored. The browser's history is never read as truth.

**Message ids and idempotency.** The run-start key is the **last user message's id**, scoped to the derived thread. It must be 1 to 128 of `[A-Za-z0-9_.-]` (`invalid_request` otherwise) and is recorded as `user_input.client_message_id`. A run started through a UI route writes a `run_receipts` row: `operation "ui"`, `idempotency_key "<thread_id>:<message id>"`, `run_id` the user_input's `event_id`, and `body_hash` = sha256 of the canonical JSON of `{agent: <thread_started.agent_name>, input: <text>, source: "api"}`. A thread's `ui` receipts are read by the byte range `>= "<thread_id>:"` and `< "<thread_id>;"`. A message whose run exists is found, in order: by its receipt; else as the `event_id` of a `user_input` of this thread. Either match checks the text (the receipt's `body_hash`; the event's text, or its text parts joined). The same text replays that run; another text is `idempotency_key_reused`. A user message's text is its text parts (AI SDK) or its string content or text parts (AG-UI), joined with no separator; any other part kind is `invalid_request`, and an empty text is `invalid_request`.

**AI SDK** `{id, messages, trigger, messageId?}`:
- `trigger: "regenerate-message"` is `invalid_request` ("fork the thread").
- A last message of role `user` starts or replays its run; the stream starts at the run's start.
- A last message of role `assistant` never starts a run and takes no receipt. It records, in order: for each tool part (`tool-<name>` or `dynamic-tool`) in state `approval-responded`, its `approval.approved` as a grant or denial of challenge `approval.id` (with `approval.reason` for a denial); for a `tool-ask_user` part in state `output-available` whose `toolCallId` is an open question, its `output` as the answer (it must parse as `Answer.answer`). Each goes through the host's approval and answer controls, as the principal. A challenge the log already decided with the same value is a no-op; another value is `approval_duplicate`. An answer to a question no longer open is ignored. The stream then starts at the frame after the **last event it newly recorded** (under the run that event belongs to, same `messageId`), which the client appends to its assistant message. With nothing newly recorded (another tab or another approver decided first, or a repeated request) it starts after the last logged decision or answer of the message's parts, which is what the tab that recorded them got; with none of those, it is the thread's latest run from its start. The answer is **204** only when the thread has no run: the stock `DefaultChatTransport` fails on a POST without a body. A decision that fails because someone settled the interrupt after the request read the log is judged against the log read again: the same value is a no-op.

**AG-UI** `RunAgentInput` (`threadId` is the key):
- A non-empty `tools` is `invalid_request` ("define tools on the agent"). `state`, `context` and `forwardedProps` are ignored. `runId` is only echoed in `RUN_STARTED` and `RUN_FINISHED`.
- The last message of role `user` is the input. Without `resume`: a message whose run exists replays it; a new one starts a run.
- Each `resume` entry `{interruptId, status, payload?}` names an interrupt. An id that names no approval challenge and no question of the thread is `invalid_request`; a park only an operator settles (an unknown effect, a resource) is `forbidden`. For the rest:
  - **open** (an undecided, unexpired challenge; a parked question): `resolved` needs a `payload` that parses as `ApprovalDecision` (approval) or `Answer` (question), else `invalid_request`; `cancelled` denies an approval, or requests a turn cancel for a question. Recorded through the same controls as the REST routes.
  - **settled** (decided with either value, answered, closed, or an expired challenge): nothing is recorded, and it is never an error. Exception: `cancelled` for an **expired challenge the branch is still parked on** requests a turn cancel, since the loop never closes an expired approval's park and the thread could not move on otherwise. One `CUSTOM{name: "threads.resume_conflict", value: {interruptId, recorded}}` is sent for each settled entry whose value differs from the log: `recorded` is `granted`, `denied`, `expired`, `answered` or `cancelled`; a `resolved` grant differs from `denied`, a denial (or `cancelled`) from `granted`, `resolved` from `expired` and `cancelled`, `cancelled` from `answered`, and an `Answer` from `answered` when its text (a list joined with `\n`) differs from the recorded answer. An entry open when the request read the log but settled by someone else before it recorded (the control answers `approval_duplicate`, `approval_expired` or `no_open_question`) is settled too: the host reads the log again and every entry is judged against it, so the race is never an error either.
  - With a **new** user message (one whose id finds no run): accepted when every entry is settled and no interrupt of the thread is still open (an expired challenge a `cancelled` entry cancels counts as settled); its conflicts follow `RUN_STARTED` and the message starts its run. Otherwise `invalid_request` ("answer the interrupts first").
  - Without a new message: the open entries are recorded, then the run the message names (else the thread's latest) is **replayed** (below).

## Mapping

`frames(p, e, before)` maps one committed event `e` of a run, given the run's events before it, to ordered frames `0..n-1` with SSE ids `<seq>:<k>`. It reads nothing else. A run's events are its `user_input` through its end (`../README.md`, "Run completion"), or, while it goes on, through the event before the next `user_input`.

- **Part ids.** A text or reasoning part's id is `P = <model_request event_id>:<i>`, `i` its index in the response's `content`. The request is committed before it is sent, so live and committed frames share the id.
- **Turn requests only.** A `model_request{purpose: compaction}`, and a response or abandonment of one, map to nothing. A request not found among the run's events counts as a turn's.
- **Shown parts.** `text` parts with non-empty text; `reasoning` parts with a non-empty `summary`; `tool_use` parts. Other parts map to nothing.
- **Proposed calls.** A `tool_result`, `tool_result_late`, `approval_requested`, `approval_granted` or `approval_denied` maps to frames only when its `call_id` is a `tool_use` of a turn response earlier in the run: the client holds that part.
- **Children.** `agent_finished` maps to frames only when its `agent_spawned` is earlier in the run.

| Event | AI SDK | AG-UI |
|---|---|---|
| `model_request` (turn) | `{type: "start-step"}` | `{type: "STEP_STARTED", stepName: "model"}` |
| `model_response` / `_recovered` (turn), per shown part in content order | text: `text-start{id: P}`, `text-delta{id: P, delta: text}`, `text-end{id: P}`; reasoning: the same with `reasoning-*` and `delta: summary`; tool_use: `tool-input-available{toolCallId: call_id, toolName: name, input}` | text: `TEXT_MESSAGE_START{messageId: P, role: "assistant"}`, `TEXT_MESSAGE_CONTENT{messageId: P, delta}`, `TEXT_MESSAGE_END{messageId: P}`; reasoning: `REASONING_START{messageId: P}`, `REASONING_MESSAGE_START{messageId: P, role: "reasoning"}`, `REASONING_MESSAGE_CONTENT{messageId: P, delta: summary}`, `REASONING_MESSAGE_END{messageId: P}`, `REASONING_END{messageId: P}`; tool_use: `TOOL_CALL_START{toolCallId, toolCallName, parentMessageId: <request event_id>}`, `TOOL_CALL_ARGS{toolCallId, delta: <canonical JSON of input>}`, `TOOL_CALL_END{toolCallId}` |
| … then | `{type: "finish-step"}` | `{type: "STEP_FINISHED", stepName: "model"}` |
| `model_attempt_abandoned` (turn) | `data-attempt{id: <request event_id>, data: {status: "abandoned", reason}}`, `finish-step` | `CUSTOM{name: "threads.attempt_abandoned", value: {requestId, reason}}`, `STEP_FINISHED{stepName: "model"}` |
| `approval_requested` | `tool-approval-request{approvalId: challenge_id, toolCallId}` | nothing (an interrupt at the park) |
| `approval_granted` / `_denied` | `tool-approval-response{approvalId, approved, reason?}` (`reason` when the event has one) | nothing |
| `tool_result{origin: deferred}` | `tool-output-available{toolCallId, output: preview, preliminary: true}` | nothing |
| `tool_result{origin: denied}` | `tool-output-denied{toolCallId}` | `TOOL_CALL_RESULT{messageId: event_id, toolCallId, content: preview, role: "tool"}` |
| `tool_result` / `tool_result_late`, `is_error` | `tool-output-error{toolCallId, errorText: preview}` | the same `TOOL_CALL_RESULT` |
| `tool_result` / `tool_result_late`, otherwise | `tool-output-available{toolCallId, output: preview}` | the same `TOOL_CALL_RESULT` |
| `agent_spawned` | `data-subagent{id: child_thread_id, data: {agent: agent_name, status: "running"}}` | `SUBAGENT_STARTED{subagentRunId: child_thread_id, name: agent_name, parentToolCallId: call_id}` |
| `agent_finished` | `data-subagent{id, data: {agent, status}}` | `completed`: `SUBAGENT_FINISHED{subagentRunId, outcome: {type: "success"}}`; else `SUBAGENT_ERROR{subagentRunId, message: "subagent <agent> ended: <status>", code: status}` |
| `retry_scheduled` | `data-status{id: "status", data: {status: "retry_wait", until: not_before}}` | `CUSTOM{name: "threads.retry_wait", value: {until: not_before}}` |
| any other event | nothing | nothing |

## Opening and closing

Frames without an SSE id (envelope frames) come from the route and the run's outcome, never from connection state.

- **Opening.** AI SDK `start{messageId: <run_id>}`; AG-UI `RUN_STARTED{threadId, runId}` (the client's ids on a POST, the threads ids on the cursor route).
- **Closing**, once the run's outcome (`host-api` `RunOutcome`, read from the log) exists. First what the log shows still open, in this order: a turn `model_request` with no response and no abandonment (`finish-step` / `STEP_FINISHED{stepName: "model"}`); each legacy subagent started in the run and not finished, in start order (parked: AI SDK `data-subagent` with status `running`, AG-UI `SUBAGENT_FINISHED{subagentRunId, outcome: {type: "suspended"}}`; otherwise AI SDK status `stopped`, AG-UI `SUBAGENT_ERROR{subagentRunId, message: "subagent <agent> stopped: <run status>"}`); then each part this connection opened live and did not close, in the order opened (`text-end{id}` / `TEXT_MESSAGE_END{messageId}`). Then:

| Outcome | AI SDK | AG-UI |
|---|---|---|
| `completed` | `finish{finishReason: "stop"}`, or `"content-filter"` when the run's last turn response stopped with `refusal` | `RUN_FINISHED{threadId, runId, outcome: {type: "success"}, result: output}` (no `result` when the output is null) |
| `parked` | `finish{finishReason: "tool-calls"}` when every pending park is an approval or a question, else `"other"` | `RUN_FINISHED{threadId, runId, outcome: {type: "interrupt", interrupts}}`, one per pending park in order (below) |
| `cancelled` | `abort{reason: "cancelled"}` | `RUN_FINISHED{threadId, runId, outcome: {type: "cancelled"}}` |
| `failed`, `budget_exhausted`, `handed_off` | `error{errorText: "<status>: <code>"}`, then `finish{finishReason: "length"}` for `failed: max_output`, else `"error"` | `RUN_ERROR{message: "<status>: <code>", code: <status>}` |

`<code>` is the failure's `error.code`, the budget's `limit`, or the handoff's `to_thread_id`. **Interrupts**: an approval park `{id: challenge_id, reason: "tool_approval", toolCallId, message: "approve <tool name>", responseSchema: <host-api ApprovalDecision>, expiresAt: <expires_at as ISO 8601 UTC with milliseconds>}`; a question park `{id: call_id, reason: "user_input", toolCallId: call_id, message: <input.question, else "">, responseSchema: <host-api Answer>}`; any other park `{id, reason: <its park reason>}`. The two response schemas are the host-api `$defs` verbatim. The AI SDK stream then ends with `data: [DONE]`.

## Cursors

A frame id `s:k` names frame `k` of event `s`. `s` must be an event of the run and `k` below its frame count, else `invalid_cursor`. **AI SDK** resumes with frames `k+1..` of event `s`, then the events after it: no gap and no duplicate. **AG-UI** resumes at the event boundary after `s`, whatever `k` is, as a new AG-UI run: `RUN_STARTED`, then the replay opening below with the snapshot point at `s`. A client starts a fresh verifier for it.

## Replays and the snapshot (AG-UI)

The stock AG-UI client reuses a message or tool call whose id it holds and appends to it, so a replayed frame would double its text. Every AG-UI stream that does not start at the run's first event is a replay: a receipt or event-id retry, the stream after a `resume`, and a cursor stream with `after`. After `RUN_STARTED` it sends, with no SSE id:

1. **`MESSAGES_SNAPSHOT{messages}`** at the snapshot point `h`: the head read after the connection registered with the live hub, but never past the run's end (the cursor's `s` on the cursor route). `messages` is `foldAgUi` of the thread's resolved chain through `h`: for each `user_input`, push `{id, role: "user", content: <text>}` where `id` is its `client_message_id`, else its `ui` receipt's message id, else its `event_id`; then apply the frames (this mapping) of each event after it, up to the next `user_input` or `h`. `foldAgUi` is `@ag-ui/client@1.0.0`'s `defaultApplyEvents` for these events: `TEXT_MESSAGE_START` and `REASONING_MESSAGE_START` add `{id, role, content: ""}` unless the id exists (role `assistant` / `reasoning`); `*_CONTENT` appends `delta` to its message; `TOOL_CALL_START` (unless the call exists) adds `{id, type: "function", function: {name, arguments: ""}}` to the assistant message `parentMessageId` names, adding `{id: parentMessageId, role: "assistant", toolCalls: []}` when there is none; `TOOL_CALL_ARGS` appends to the call's `arguments`; `TOOL_CALL_RESULT` inserts `{id: messageId, role: "tool", toolCallId, content}` right after the assistant message holding the call and any tool messages already after it; a nested `MESSAGES_SNAPSHOT` merges by id (kept in place and replaced when named, dropped when not, a reasoning message kept when the snapshot has none, new ones appended). The stock merge of this snapshot therefore yields the fold itself. Cost: linear in the thread's history per replay.
2. The `threads.resume_conflict` frames of the request, if any.
3. **The open-state preamble** at `h`: `STEP_STARTED{stepName: "model"}` when a turn request is open, then `SUBAGENT_STARTED` for each running legacy subagent, in start order.
4. The frames of every event after `h`, with live text as below, and the closing frames.

The AI SDK needs no snapshot: its reconnect and retries restart at `start{messageId: <run_id>}`, and the client replaces the message with that id.

## Live text

Deltas are best-effort and never logged. An adapter's `ModelChunk` delta names `part`, the index its text has in the committed `content` (an adapter that can't name it sends none). The loop redacts each part's deltas with a redactor of its own, so a part's live text is always a prefix of its committed text; a part's held tail is released when a later part begins or the response completes. Only the start and reconnect streams served by the process running the model carry live frames; the cursor route never does.

**The live hub.** Per thread, the process records for each `(request, part)` that a delta went out, until the request's response or abandonment is appended. A connection registers with the hub **before** it reads the head or starts the run; the parts already started then are **missed** and never stream live on it. Deltas of a request whose `model_request` the connection has not processed yet wait until it has; deltas after the request's response or abandonment are dropped.

**Per connection, per part `P` (index `i`) of request `R`:**

| When | The connection sends |
|---|---|
| the first delta of `P` it sees, `P` not missed, and (`i` is 0 or part `i-1` of `R` streamed live here), and live streaming of `R` has not stopped here | `text-start{id: P}` / `TEXT_MESSAGE_START{messageId: P, role: "assistant"}`, then the delta as `text-delta{id: P, delta}` / `TEXT_MESSAGE_CONTENT{messageId: P, delta}`; it records `sent[P]` |
| a later delta of a part it streams | the delta; `sent[P]` grows |
| the first delta of a part that fails the rule above | nothing, and live streaming of `R` stops on this connection: every later part of `R` arrives at commit, so the live parts are the leading text parts and keep model order |
| a delta of a missed or refused part | nothing |
| the committed response `s`; a live part's canonical frames `s:a` (start), `s:a+1` (content), `s:a+2` (end) | skip `s:a`; send `s:a+1` with `delta` the committed text after `sent[P]` (skipped when empty); send `s:a+2`. Other parts' frames are canonical |
| the commit shows a live part is not a shown text part, or its text does not start with `sent[P]` | `text-end` / `TEXT_MESSAGE_END` for each part of `R` open live, then the connection **closes without `[DONE]`**; the client falls back to a replay |
| `model_attempt_abandoned` of `R` with parts open live | `text-end` / `TEXT_MESSAGE_END` for each, then the event's canonical frames |

A skipped frame's id counts as sent. After any frame `s:k` the client's message equals the canonical stream's through `s:k`. The one difference between live and replay is an abandoned attempt's live text: a live UI keeps it (closed, marked by `data-attempt` / `threads.attempt_abandoned`) until the next replay, which does not contain it.
