# threads v0.1 — the complete goal list

Updated 2026-09-26 against `origin/main` at `a48b8a32`. Sources: `README.md`, `AGENTS.md`, `plans/north-star.md`,
`plans/specs/v01-scope-expanded.md` (the release gate), `plans/specs/v02-architecture-api.md` (Gate 1),
`plans/specs/lanes/README.md`, `plans/reviews/codex-backlog.md`, `plans/reviews/followups.md`,
`plans/reviews/codex-v01-release-test-2026-09-26.md`, `plans/research.md`, `plans/market-research.md`,
`plans/structure.md`, `plans/codex_claude_talkto.md` (#426–#430), and the coordinator's memory notes.
Status claims were checked against `git log`, `spec/api.json`, `spec/api-surface-gaps.json` and the source
tree, not against the lane docs alone. A passed test proves that test's scenario; it does not make a wider
release gate complete.

**Scope decision, user, 2026-09-26: A2A and teams Phase 2 are in v0.1, and there is no v0.2 at all.**
Everything below either ships in v0.1 or is a permanent non-goal. Nothing is "deferred to the next
release", because there is no next release to defer to. Revised accordingly on 2026-09-26.

Lines marked **(inference)** are mine, not stated by the user or an approved spec.

**How to read this plan:** threads is a library that runs an agent from your application and records the
run as an event log. Section 2 describes the API v0.1 must provide; it is the target, not a list of features
available today. Section 5 is the current checklist. **Built** means the code exists, **on main** means it has
landed in the shared branch, **off-main** means work is still being reviewed or developed elsewhere, and a
**gate** is a requirement that must be complete before the named release can ship.

---

## 1. The one-sentence goal

**Ship a framework in TypeScript and Python from one spec. The agent loop runs in your process, tools cross
into an isolated sandbox, channels, memory, knowledge, hooks and MCP are included, and every run is an
append-only log you can replay, fork, resume and turn into a test.**

The README hero states it as **"Agents you can inspect, replay and trust."**

**Who it is for:** developers building agent apps that touch real systems — code, PRs, messages, money — in
TypeScript or Python, who have already been burned by an opaque failure or a repeated side effect. The user's
framing: *"companies keep rebuilding sandboxes, Slack/WhatsApp integrations, hooks, knowledge bases and memory.
threads provides them via config + API keys so nobody rewrites them"* (user, 2026-09-23). The marketing line
the user approved: **"Stop rebuilding the same agent plumbing."**

Not for: people who want to declare a graph. `AGENTS.md` invariant 8 — event-log style only, no
graph/node/edge abstractions.

---

## 2. Product goals

Each line is a capability a user can exercise at v0.1, with the API that delivers it. `spec/api.json` is the
contract: 35 public functions, 19 packages, mapped name-for-name across both languages.

A row marked **not built** is a v0.1 gate that has not landed yet, not a maybe. Section 5 says where
each one stands.

### 2.1 Core loop

| Goal | API |
|---|---|
| Define an agent in one call and run it | `agent({name, instructions, model, sandbox, tools, …})`, `await a.run(task, {store})` |
| Run from sync Python | `a.run_sync(task, store=…)` |
| Typed structured output, with retries and a fallback model | `agent({output, outputRetries, fallback})` — both languages |
| Re-open a past thread and act on it | `openThread(store, id)` → `Thread` |
| Read every step of a run | `thread.timeline()`; `threads timeline <id>` |
| Replay every recorded request with zero model calls | `thread.replay()` |
| Compact the context deliberately | `thread.compact()` (idle only; the next run carries it out) |
| Change the answer style without touching the prompt prefix | `thread.setOutputStyle()` + `agent({outputStyles})` |
| See what a run cost | `thread.usage()`, `thread.cost({tree})`, `thread.cacheBreaks()` |
| Cancel a running thread | `thread.cancel(principal)` |
| Keep secrets out of prompts, logs and sandboxes | `secret("ENV_NAME")` |
| Pick a model by name, with provider-verified limits | `anthropic("claude-sonnet-5")`, `openai("gpt-5.5")`, `litellm(…)` (Py), `aiSdk(…)` (TS) |

### 2.2 Tools

| Goal | API |
|---|---|
| Write a tool whose Zod/Pydantic schema is the type, the validator and the model-facing JSON Schema | `tool({name, input, execute})` |
| Get shell, file, search, web, git, code-intelligence, notebook and computer-use tools without writing them | the built-in catalog (ADR 0021), one Zod source |
| Run independent read-only calls of one response together | `tool({concurrent})` — max 8 in flight, recorded in call order; effectful calls stay barriers |
| Attach any MCP server in one line | `mcp({name, url})` |
| Carry hundreds of tools without paying prefix cost | `tool({defer})`, `mcp({defer})`, `agent({deferTools})` + the pinned `tool_search` tool |
| Ask a human a question mid-turn, durably | the `ask_user` tool; 24 h expiry; the asker's next reply answers it |
| Keep a task list the model maintains | `todo_write` |

### 2.3 Sandboxes

| Goal | API |
|---|---|
| Give an agent its own Linux machine | `sandbox: e2b()` / `daytona()` / `modal()` (Python only) |
| Keep credentials out of it | invariant 4; credential-needing tools go through a host gateway |
| Keep the network shut unless asked twice | `e2b({allowInternet: true})` **and** `egress: "unenforced"` |
| Move a workspace in and out as bytes | `exportTree` / `importTree`, tar read by a strict parser |
| Fork into a fresh sandbox restored from a snapshot | `thread.fork(point)` — **Daytona only today**; E2B and Modal have no snapshots |

### 2.4 Channels and host

| Goal | API |
|---|---|
| Put an agent behind Slack, WhatsApp or GitHub | `host({agents, channels: {slack: slack({…})}})`, then `threads dev` prints the webhook URL |
| Run it in production | `threads start` |
| Drive it over HTTP with live streaming | the host HTTP API (`spec/schema/host-api/`) + per-run SSE |
| Run an agent on a schedule | host schedules — one thread per schedule, rotation-safe |
| Approve or deny a pending call from the channel it came from | approvals bound to actor, exact call, arguments and branch |

Channels are never an `agent()` option. They bind on the host (ADR 0012 item 11).

### 2.5 Memory and knowledge

| Goal | API |
|---|---|
| Local memory with no service | `memory: localMemory()` (SQLite, in the run's store) |
| Swap in a hosted provider | `supermemory()`, `zep()` — `mem0()` refuses at setup (its SDK sends data outside the fence) |
| Search your own docs, versioned and citable | `knowledge: localKnowledge({paths})` — ingest → rebuildable index → authorized search → excerpts recorded → citations resolve to the exact revision → replay uses recorded results |
| Never let recalled text act as an instruction | invariant 6: recall is injected as untrusted reference |

### 2.6 Multi-agent

The user's top priority, in their words: *"many agents talking to each other ... async all that ... is core,
but strands does, so how come we missed it"* (2026-09-23). Also Erlang-style: processes, mailboxes,
supervisors, hot reload.

| Goal | API |
|---|---|
| Give a lead a team | `agent({team, teamLimits: {concurrent, mailbox}})` → `run()` returns `RunResult & {team: Team}` |
| Let the model start, message, ask, wait on and cancel peers | model tools `start send ask reply wait monitor cancel` |
| Drive the same team from code | `team.start/send/ask/wait/cancel/members/events/askStatus`, `openTeam(store, ref, {principal})` |
| Let a lead define its specialists at start, inside a template's limits | `dynamicAgent(template)` |
| Wake an idle lead when a helper finishes | `woken` + the implicit task monitor |
| Keep every message, refusal, timeout and outcome on record | the team log; every index row rebuildable from event bytes |
| Watch a team live | `team.events({after, follow})` and `GET /v1/teams/{team}/events` — lane 29B, not built |
| Restart a crashed member under a policy | `host({members: {…, restart, maxRestarts}})` — lane 29E, not built |
| Say who may message whom | `host({messagePolicy})`, default deny — lane 29C, not built |
| Reach an agent in another company | `a2a.expose(...)` to publish, `remote(url, {bearer})` to call — lane 30, not built |

Phase 1 (one machine, many processes) is built. Phase 2 (lane 29) and A2A (lane 30) are spec'd, not
started, and **required for v0.1** (user, 2026-09-26). Team fork and team saved cases are a
**permanent non-goal**: they need a consistent cut across many logs, and no gate here buys one.
Per-member fork and saved cases work.

### 2.7 Evals

The user's headline: easy evals, real runs become tests.

| Goal | API |
|---|---|
| Turn any completed turn into a committed regression case | `thread.saveCase(name, {expect, externalEffects: "stub", rubric})` |
| Run every saved case in CI for free — no model, no keys, no network | `threads eval --agent ./agents.ts`; `runEvals()` / `run_evals()` |
| Know which cases a prompt/tool/model change touches | the free drift check (`--agent`, a dry pin vs the saved line-0 bytes) |
| Grade a rubric with a judge after a real change | `threads eval --live`, under a budget you set |
| Continue a saved conversation with a simulated user | `saveCase({simulate: {persona, goal, maxMessages}})` — **spec'd, not built** |

### 2.8 Observability

| Goal | API |
|---|---|
| No telemetry unless asked | nothing is exported by default (market complaint 9) |
| Export spans derived from the log to any OTLP collector | `host({telemetry: otel()})`, `sync()`; standard `OTEL_*` honoured |
| Serve a chat frontend without writing a protocol | `GET /v1/ui/{protocol}/{agent}` — AI SDK UI message stream and AG-UI 1.0, approvals and `ask_user` through each protocol's native human-in-the-loop |

### 2.9 Storage

| Goal | API |
|---|---|
| Local by default, zero setup | `store: sqlite(".threads")` |
| The same contract on a real database | `store: postgres(url?)`, serializable transactions on both engines |
| Export and re-import a branch byte-for-byte | `threads export <branch>` / `threads import <file>` |
| Inspect, repair, gc and delete | `threads timeline / repair / gc / delete` |

### 2.10 Frontends and presets

| Goal | API |
|---|---|
| A batteries-included coding agent in one call | `codingAgent()` / `coding_agent()` in `@threadsai/coding` — **spec'd, blocked on lane 16 D/E** |
| A local container sandbox with no cloud account | `docker({image, cpus, memoryMb, allowInternet})` — **spec'd (lane 16D2), not built** |
| Docs you can copy from and have work | every example and docs code block runs in CI (lane 11) — **not built** |

---

## 3. Guarantee goals

The eight `AGENTS.md` invariants, each as a promise and the thing that proves it. All eight are live on main.

1. **"Your log is the truth."** The log is append-only and the only source of truth; state is `reduce(log)`.
   *Proof:* 55 semantic rules in `spec/schema/README.md`, each with at least one conformance case (~1,483 case
   files); replay tests reduce an old fixture log to the same state after any change; a TS-written log reduces
   identically in Python and back.
2. **"Two copies of your agent can't both act."** One writer and one executor per branch; only the current
   lease holder (fencing epoch) dispatches model calls or effects. A stale owner is rejected.
   *Proof:* `python/tests/loop/test_invariants.py::test_a_stale_owner_never_sends_a_durable_model_request`,
   `…never_dispatches_a_begun_effect`, `…a_lease_lost_while_the_send_is_queued_sends_nothing`, and the TS pair.
3. **"A side effect is never silently repeated."** `effect_begin` is durable before dispatch; a dispatched
   attempt is *potentially sent* until settled by provider dedup, an adapter proving nothing was sent, a final
   reconciliation, or a human. Anything else parks. Recovery re-checks approval, cancellation and policy. A
   human retry is recorded as accepting duplicate risk. **Nothing is ever called exactly-once.**
   *Proof:* `test_intent_is_durable_before_every_dispatch` (property test over step orders), crash drills in
   `tests/team/crash_kit.py`, `host/test/question-send-crash.test.ts`, `card-send-crash.test.ts`, the jobs drills.
4. **"Your keys never enter the sandbox."**
   *Proof:* per-adapter `credentials.test.ts` / `test_credentials.py` in every provider package; the preset's
   `env` and `/proc/1/environ` checks.
5. **"The prompt prefix is byte-equal on every request of a settings epoch."** Render v1 line 0 (system, tools,
   pinned instructions, model and adapter settings) changes only through an authorized `settings_changed`,
   never by resetting a baseline after a mismatch. A starts-with check does not count.
   *Proof:* `test_every_request_of_an_epoch_declares_the_same_line0`; render golden snapshots; `tools_loaded`
   is a history line so loading a deferred tool never touches line 0.
6. **"Recalled text is never an instruction."** Memory, knowledge and summaries are injected as untrusted
   reference. *Proof:* conformance cases for framed recall; the dynamic-subagent block check (delimited,
   last, operator-precedence sentence).
7. **"The agent cannot write its own config, skills or hooks."** *Proof:* fail-closed tests on every exposed
   write path, re-run per lane; the `bash(*)` rule is consulted only after the self-config guard.
8. **"No graph to declare."** *Proof:* there is no node/edge/state-machine API in `spec/api.json`.

**Two guarantees are deliberately bounded, and must stay stated in the docs:**
- A thread pinned to an old config resumes only on a host still serving that config (lane 14C "As built" item 9;
  Codex #427 accepted it as a documented deployment limit, retiring the earlier unconditional claim).
- Telemetry is at-least-once for retained threads; a deleted thread's unexported spans are counted as
  *possibly lost*, not guaranteed delivered.

---

## 4. Quality goals

- **Merge bar:** two scores out of ten — code quality and API readability — with no open HIGH. **≥ 8 on both**
  (lowered from 9 by the user on 2026-09-24). User's rule: *"no god files at least not above 800-900 lines and
  readability should be there for api, its framework for humans."*
- **Reviewer:** Codex on high-risk lanes (invariants, security); otherwise a fresh-context Claude reviewer plus
  the coordinator's own diff read and a hands-on probe. Every lane merged without Codex is listed in
  `plans/reviews/codex-backlog.md` for re-review — 24 entries open.
- **Deviation rule:** *"features shouldn't diverge what we asked."* User-visible deviations from an approved
  spec go to the user before merge; internal-only ones may be approved by the reviewer.
- **File size:** CI fails any hand-written source file over **400 lines** (`scripts/check-file-size.sh`),
  stricter than the user's 800–900. Over ~300 is a smell.
- **Complexity:** ruff `C90` max 10 plus `PL` limits; Biome cognitive complexity max 15. Split, never raise.
- **Parity:** one spec, two implementations, conformance in both. Public names mapped in `spec/api.json`; a
  CI surface gate proves every spec'd factory, option and method exists in both languages and is tested.
  Open parity gaps are tracked in `spec/api-surface-gaps.json` and must reach **zero** before release.
- **Types:** TS — no `any`, no `as` (except `as const`), no `!`, no `@ts-ignore`, `isolatedDeclarations` on,
  every `switch` over a union ends in `assertNever`. Python — pyright strict, no `Any`, no `cast`, no
  `type: ignore` outside tests, frozen/strict Pydantic at every boundary, `assert_never` on every match.
- **Tests:** no test ever hits a real model (scripted model + a global request guard). Bug fix = failing test
  first. Property tests on parsers, reducers and canonical JSON.
- **Docs that run:** every example and every docs code block executes in CI on the scripted model (lane 11).
- **CI gate, per language:** `tsc --noEmit` + `biome check` + `bun test`; `pyright` + `ruff check` +
  `ruff format --check` + `pytest`. Plus Postgres 17 on both languages, docs validation, the two surface
  gates, `gen_fixtures --check`, `check_api` and the file-size check.

---

## 5. Release gates

Nothing publishes until every required line is **done**. Status verified 2026-09-26 at `origin/main`
`a48b8a32`. With no v0.2, this list is the whole release: a gate is either met or the release waits.

### Already true

| # | Gate | Evidence |
|---|---|---|
| G1 | Core loop, tools, permissions, hooks, skills, compaction, todos, MCP, built-in tools in both languages | on main; TypeScript and Python CI passed at `a48b8a32` |
| G2 | Model adapters (Anthropic, OpenAI, AI SDK, LiteLLM), verified limits catalog, prompt caching, **all four in `spec/api.json`** | lanes 06, 10; the AI SDK and LiteLLM contract entries are on main |
| G3 | Sandboxes E2B, Daytona, Modal(Py); remote kit both languages; E2B on Node without the SDK; tar trees | lane 16 A1–A4, B1, B2 |
| G4 | Channels Slack/WhatsApp/GitHub, host, CLI, HTTP API, per-run SSE, schedules | on main; lane 14C for schedules |
| G5 | Memory (local, Supermemory, Zep) and local knowledge | on main |
| G6 | Log, fork, timeline, saved cases, resource ledger, cross-language log parity | on main |
| G7 | Thread handle complete: `replay`, `compact`, `setOutputStyle`, `usage`, `cost`, `cacheBreaks` | lanes 04, 17 |
| G8 | Python `output` / `output_retries` / `fallback`; `run_sync`; `runs` defaults to host; env defaults | lanes 05, 08, 07, 09 |
| G9 | Protocol optionality; `tool.concurrent`; `browser`/`stream_release`/`tool.module`/`tool.entrypoint` narrowed out of the contract | lanes 02, 18 |
| G10 | Adapter factory contract: every refusal through one audited site, complete per-language inventory | lane 15 A/B/C |
| G11 | Durable `ask_user` questions across channels, HTTP and both languages | lane 14A |
| G12 | Eval runner: `runEvals()`, `threads eval`, offline rerun, drift check, live judge | lane 22 |
| G13 | OpenTelemetry export | lane 23 |
| G14 | `tool({defer})` + `tool_search`, with line 0 untouched | lane 24 |
| G15 | Dynamic subagents | lane 26 |
| G16 | Chat frontends: AI SDK UI stream and AG-UI 1.0 per run | lane 25 Part A |
| G18 | Teams Phase 0 + Phase 1 model side: team log, mail with receipts, ask/reply/wait/monitor/cancel, budgets, provenance, deletion | lanes 21A–21E |
| G19 | API readability gate: every public name and input explained and visible in the reference | lanes 12, 20 |
| G20 | Public-readiness scan (license, secrets, no private paths on origin) | `plans/reviews/codex-public-readiness.md` |

### In progress

| # | Gate | Where it stands |
|---|---|---|
| G17 | Postgres store **and its required CI gate green** | the store is built and PG17 passed earlier. The latest required workflow is red because a SQLite Team race test sampled only one of two valid outcomes. This is not evidence of a Postgres store regression, but the release CI gate is not green |
| G21 | Operator `Team.ask / wait / cancel / askStatus` (closes teams Phase 1) | landed on main. Focused operator tests pass in both languages, but the gate stays open: fractional `wait` modes can throw, and a zero timeout behaves differently in TypeScript and Python |
| G22 | Hardening (lane 03) | CI path filters include `scripts/**`; fsync durability tests exist; lane not closed. The local Docker daemon is available again, so Linux-image checks can run locally as well as in GitHub CI |
| G23 | Codex re-review backlog | 24 lanes merged on Claude review during the Codex outage; Codex returned 2026-09-26 (#426) and has re-run focused 14C and 09 checks (#427, #428). Priority order posted in #429 |
| G24 | `spec/api-surface-gaps.json` empty (`--release` fails on any gap) | **41 gaps open**: 12 base API placement gaps, 1 Python output/fallback gap, 3 Teams Phase 1 gaps, and 25 Teams Phase 2 gaps |

### Still required for release

**The v0.1 build and release set.** Every row is required. Some rows have partial test or deployment
evidence, recorded below, but their full gate remains open. No row has a later release to fall into.

| # | Gate | Build units | Note |
|---|---|---|---|
| G25 | Lane 16 C/D/E: core-owned host tree snapshots, the injected supervisor, `docker()` on any image, the confined dev sandbox, `agent.workspace` | 16C, 16D1, 16D2, 16D3, 16E | none are on current main. The local Docker daemon is back for testing, and Python `docker()` work is active off-main; the full cross-language gate remains unbuilt and unreviewed. This gates the preset agent and is the only planned way to run without a cloud sandbox account |
| G26 | Lane 14B (stub forks), 14D (public async `thread.export` / `importThread`), 14E (a direct test per hook) | 3 | 14D is not in `spec/api.json` yet |
| G27 | **Teams Phase 2 (lane 29)**: host as team worker, team stream + SSE, host `messagePolicy`, host members, supervision, cross-process cancel | 29A, 29B, 29C, 29D, 29E, 29F | spec is on main and implementation work is active off-main; no Phase 2 implementation has landed on current main. This is where the user's "watch a whole team live" and "Erlang-style supervision" actually land. Crash drills, 1,000-schedule two-host races and replay equality are part of it |
| G28 | Lane 25 Part B: team streams over the team feed | 1 | waits on 29B |
| G29 | **A2A (lane 30)**: expose host agents (`a2a.expose`) and call remote agents as members or tools, every outbound message an `effect_begin` first, an uncertain first response parks | 30A, 30B, 30C, 30D, 30E | approved rev 2.1; the implementation is not on current main. Needs 29A, 29C, 29F, 19 and 25. This is the user's "agents across teams and companies connecting" |
| G30 | **Preset agent (lane 31)**: `codingAgent()` / `coding_agent()` in its own package | 1 | approved; blocked on G25 (16 D and E) |
| G31 | **Simulated users (lane 32)**: a scripted or model-played user continues a saved conversation in `--live` evals | 1 | approved; needs lane 22 (done) |
| G32 | Lane 13: rename to `threadsai` | 1 | packages are still `@threads/core` and `threads`; versions `0.0.0`. Everything except 03 and 11 must land first |
| G33 | Lane 11: examples and docs blocks run in CI | 1 | only `examples/evals.ts` and `examples/evals.py` exist today; lands last, after 13 |
| G34 | Live-provider pass | — | **Partial evidence only:** bounded Anthropic and OpenAI adapter calls, single-agent replay, two-member Teams runs and one read-only typed-tool call passed in both languages. The full live suite, every provider, live crash recovery, hosted channels and real external effects are still unproved. Budget: ≤ ~2–3M tokens/day |
| G35 | Docs complete per §F1 and deployed | — | production at threadsai.dev was deployed successfully with local Wrangler OAuth. Automated `docs-deploy` CI is still red because the repository lacks `CLOUDFLARE_API_TOKEN`; §F1 documentation work also remains. Manual production success does not make this release gate complete |
| G36 | npm org `threadsai` claimed, PyPI pending publisher configured, release workflow authorized | — | needs the user: names are reserved at 0.0.1, publishing still asks |
| G37 | Every `README.md` claim true, every unbuilt claim in "Coming next" | — | the stale "a runner for saved cases" line is gone on main; "agents messaging each other across threads, and the A2A protocol" comes off the list when G27 and G29 land |
| G38 | `plans/reviews/followups.md` triaged: nothing HIGH left open | — | 63 open items, most marked for a parity pass or a named lane; 2 flaky team tests and 1 HIGH-ish Python channel retry loop are unfixed |

### What this decision adds

Three lanes move from deferred into required: **A2A (30), the preset agent (31) and simulated users
(32)**. Teams Phase 2 (29) and lane 25 Part B were already gates; what changes for them is that they
can no longer be dropped to make a date.

Counting only what is spec'd and unbuilt, the remaining build set is **24 units**: 5 in lane 16
(C, D1, D2, D3, E), 6 in lane 29, 5 in lane 30, 3 in lane 14 (B, D, E), and one each for 25B, 31, 32,
13 and 11. Every one lands in **both languages**, with conformance cases where a contract covers it.

Lane 29 and lane 30 are the two largest and the two riskiest. 29 adds a host-driven worker, a new SSE
surface, a permission table and supervision. 30 adds a protocol at the network edge, inbound and
outbound, with effect-recovery semantics on every message we send.

For scale, teams Phase 1 (21A–21F, comparable in shape to 29) was six sub-lanes and took from
2026-09-24 to 2026-09-26 with lanes fanned out in parallel. G21 has landed but is still not closed
because its public invalid-input behavior needs fixes. **(inference)** A2A is the larger risk of the two,
because it is the first surface where an uncertain outcome comes from a party we do not control, and lane
30 has no build experience behind it.

The coordinator's last ETA to the user (2026-09-24) was alpha.1 in ~1–1.5 weeks and full v0.1 in
~4–6 weeks. **That estimate predates this decision and predates lane 29's spec.** I am not restating
it as a number here; what I can say is that the 4–6 week figure did not have lanes 30, 31 and 32 as
gates, and the tail is now strictly longer. A new estimate should be made against the 24-unit list
above, not inherited.

### Release order

1. **0.1.0-alpha.N** may publish once section C (friendly API) has landed — it has — and G21, G24,
   G32, G33, G34, G35, G36 and G37 are true. Alphas ship **SQLite only**.
2. **0.1.0** additionally requires everything else: G17, G22, G23, G25, G26, G27, G28, G29, G30, G31
   and G38.

The alpha is the only place where "not yet" is allowed, and only for the gates named in step 1.

---

## 6. Permanent non-goals

There is no v0.2, so this list is final: anything here is something threads does not do, not
something it does later. Chosen scope, not market findings (`plans/market-research.md`,
`plans/structure.md`).

- Our own sandbox infrastructure. Adapters only.
- A memory product, or a channel/integration catalog beyond Slack, WhatsApp and GitHub. Everything
  else is MCP.
- Hosted tracing SaaS. OTel export instead, off by default.
- A graph DSL, or crew/hierarchy abstractions (invariant 8).
- Exposing threads itself as an MCP server.
- MS Teams, OAuth connection flows, vector knowledge connectors.
- Cross-harness evals; a REPL context tool (RLM); a graphical timeline viewer.
- **Eval world drift** (`tool({world})`, `ctx.eval.asOf`, lane 33). The user shelved it on
  2026-09-24: "side angle, not in plan". With no v0.2, it is a non-goal, not a shelf.
- **Team fork and team saved cases.** They need a consistent cut across many logs; no gate in this
  document buys one. Per-member fork and saved cases work, and that is the promise threads makes.

### Named limits threads ships with

These are not features being skipped, they are boundaries the docs must state plainly. Each one has
a place in the docs today or gets one before G35 closes.

| Limit | Why, and what a user sees |
|---|---|
| **Fork needs a snapshotting provider** | Daytona snapshots; E2B and Modal do not. `fork()` on the others is refused, not silently degraded. The README already says so |
| **Egress is all-blocked or all-open** | there are no per-host allowlists. Opening the network takes two explicit settings, so it is never accidental |
| **Modal has no TypeScript adapter** | its JS SDK's transport can't be fenced, and an unfenceable route is refused at setup rather than half-supported. `modal()` in TS refuses and points at Python |
| **No interactive stdin/PTY in sandboxes** | commands are one-shot; an interactive session is its own future spec, not part of this contract |
| **No streaming tool execution** | tools run to completion before their result is appended; the Claude Code loop check put this past v0.1 and nothing since has moved it |
| **Durable code orchestration is model-directed only** | Codex decision O2 = C. A lead's model drives `start/send/ask/wait`; there is no durable code workflow engine. It needs stable step ids, code-version pinning, mediated effects and divergence detection, and none of that is in this release |
| **Phase 3 enterprise items** | supervised restart *generations*, deploy with revisions and OIDC mapping are Phase 3 in the Gate 1 design; lane 29E brings restart policies and caps into v0.1, the rest does not land. Tenant **usage** is reported from Phase 1; tenant **limits** are not enforced |
| **Docker socket authority is root-equivalent** | documented in lane 16D2, not engineered away |
| **An old-config thread resumes only on a host still serving that config** | lane 14C's documented deployment limit, in the schedules guide |
| **Telemetry is at-least-once, and lossy on delete** | a deleted thread's unexported spans are counted as *possibly lost*. The log is the record; telemetry is not |
| Anthropic native `defer_loading` + `tool_reference` blocks | a provider feature that would keep the prompt cache intact across tool loads. Not in this release; `tool_search` works without it |

---

## 7. Open questions for the user

1. **Package names.** Memory records the decision: unscoped `threadsai` on npm, `@threadsai/*` for
   adapters, `threadsai` on PyPI with extras. `v01-scope-expanded.md` §F2 still says `@threadsai/*`
   for the core and leaves hyphenated `@threads-ai/*` / `threads-ai` "pending the user's call".
   Confirm so lane 13 runs once, not twice.
2. **Claim the npm org and configure the PyPI pending publisher** — and decide whether the release
   workflow publishes, or the coordinator publishes locally with tokens. Publishing still asks.
3. **Add the `CLOUDFLARE_API_TOKEN` repo secret**, or explicitly accept local Wrangler OAuth as the
   release process. A manual production deployment succeeded; `docs-deploy` CI still fails at Wrangler.
4. **Full live-provider pass:** bounded Anthropic/OpenAI agents, Teams and a read-only tool have passed.
   Choose the remaining provider/model matrix and confirm the ≤ 2–3M tokens/day budget covers the full
   `THREADS_LIVE=1` suite plus the eval judge smoke run (lane 32's worst case is ≈ 0.9M tokens/day).
5. **A2A interop partner.** Lane 30 proves itself against the A2A 1.0 spec and our own expose route.
   Is there a real third-party agent you want it tested against before release? Self-interop is a
   weaker proof than the "agents across companies" goal deserves. **(inference)** worth one.
6. **Fork on E2B and Modal.** Fork is a headline promise and works on Daytona only. Ship with that
   limit stated, or hold the release until a second provider snapshots? **Recommendation
   (inference):** ship with the limit stated; the README already says it.
7. **Should a member that ends bounce `member_ended` for every unanswered ask it received?** Today
   they wait out the deadline — up to 120 s on a channel. The coordinator leans yes, before 29D
   (talk file #429).
8. **Rule 53** (`turn_failed` bounce followed by `member_ended` with no `member_idle`): the reference
   implementation accepts it, the rule text does not. One must change before 29D.

---

## Appendix: contradictions found, and what was done about each

Every one was a real inconsistency, not a wording nit. All but two are now fixed.

| # | Contradiction | Resolution |
|---|---|---|
| 1 | **A2A in or out of v0.1** — `v01-scope-expanded.md` §E listed it "In" and §F2 said v0.1.0 ships when A–F are done, while `README.md` put it under "Coming next" and the lane order put it behind all of teams Phase 2 | **Fixed by the user, 2026-09-26: in v0.1, and there is no v0.2.** Written into `v01-scope-expanded.md` (header + D3 + D6 + a tail order) and `lanes/README.md` (header + the 29/30/31/32 rows) |
| 2 | **`@threads/ai-sdk` and `litellm()` were not in `spec/api.json`** — both documented in `README.md`, both in the tree, neither in the contract, so the surface gate could not check their options, refusals or defaults. They were tracked as `pending` in `api-surface-factory-decisions.json` under lane 06, which closed without installing them | **Fixed on main:** both are installed with full option sets, `config_errors` and coverage rows; both generators allow a package to be absent from one language; the docs reference is generated from the contract |
| 3 | **README stale on evals** — "Coming next: A runner for saved cases", two sections after documenting `threads eval` and `runEvals()`, which lane 22 shipped | **Fixed** (the line is gone; it landed in commit `4e41c986`, see the note below) |
| 4 | **Lane file headers contradicted the lane index** — `11-examples-as-tests.md` and `12-readability-gate.md` both said "Draft … Not approved to build" while `lanes/README.md` marked both approved, and lane 12 was merged and pushed on 2026-09-24 | **Fixed**: both status lines now say what is true, and name `lanes/README.md` as the authority |
| 5 | **Lane 14C's review record** — Codex approved round 4 (#424), then revised it to REQUEST CHANGES (#425); the backlog read like a clean approval | **Fixed**: the backlog line now says #424 was revised, that the round-6 Claude review is the merge evidence, and that #427 settled the remainder as a documented deployment limit |
| 6 | **Quality bar drift** — recorded as 9 (2026-09-23) then 8 (2026-09-24); `lanes/README.md` carried "the bar was 9 until 2026-09-24" | **Fixed**: one number, 8, in both `lanes/README.md` and `review-checklist.md`, with the history named once so it can't be re-litigated |
| 7 | **File-size bar** — the user said "not above 800-900 lines"; `AGENTS.md` and CI enforce 400 | **Fixed**: `lanes/README.md` now states 400 beside the score bar. The stricter rule was always the one in force; now it is the only one written down |
| 8 | **north-star.md says "13 families" and lists 14** (knowledge base is item 14) | **Open.** Cosmetic; left alone because north-star.md is a record of an agreed direction, and renumbering it would edit history for a counting slip |
| 9 | **`structure.md`'s release split** (v0.1 = Phase 0 + 1; timeline viewer in v0.2; skills/subagents in v0.3) is superseded by `north-star.md`, which says so at the top — yet `structure.md` still carries a "v0.1 gates" list that no longer matches | **Open.** It is explicitly marked superseded at the top; this document is the current gate list. Worth deleting the stale gate list if anyone reads `structure.md` cold |

**Historical note:** `4e41c986` is a pre-rewrite commit id. The history rewrite changed commit ids;
the README fix is on current main.
