# threads

An agent harness where every run is an append-only event log.

The log is the only source of truth. Four things fall out of it:

- **Durability** — replay the log; completed steps return recorded results.
- **Timeline** — render the log; every context injection is an event.
- **Fork** — copy the log up to a completed snapshot boundary and restore that snapshot; any other boundary is rejected.
- **Evals** — fork at N, run live, assert on the new events.

## Kernel

```ts
type Event =
  | { t: "user"; text: string }
  | { t: "model"; reqHash: string; res: ModelResponse }
  | { t: "tool_call"; id: string; name: string; input: unknown }
  | { t: "tool_result"; id: string; ref: ArtifactRef }
  | { t: "effect"; key: string; status: "begin" | "commit" }
  | { t: "injected"; source: string; text: string }
  | { t: "snapshot"; sandboxId: string }
  | { t: "approval"; callId: string; granted: boolean }
  | { t: "compacted"; summaryRef: ArtifactRef };

reduce(log)               // -> messages, todos, memory refs
fork(log, n)              // log prefix, only at a completed snapshot boundary
replay(log, { from: n })  // recorded before n, live after
```

Tools, memory, skills, and sandboxes only ever append events.

## Invariants

1. Side effects never silently repeat (`effect` begin/commit with an idempotency key). Exactly-once where the effect class allows it; otherwise the outcome is recorded as `unknown` and parked.
2. Credentials never enter the sandbox.
3. The prompt prefix stays byte-identical across turns (cache stability).
4. Memory recalled into context is labeled untrusted reference, never instructions.
5. The agent cannot write its own config or skills.

## Status

Pre-alpha. Nothing to install yet.
