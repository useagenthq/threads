# AGENTS.md

Rules for anyone (human or agent) writing code in this repo. Read `README.md` first for the design.

## Design idea

Pydantic-level strictness in TypeScript: **one schema is the type, the validator, and the tool JSON Schema.**

- Every data shape is a Zod 4 schema. The TypeScript type is `z.infer<typeof Schema>`. Never hand-write a type that duplicates a schema.
- Tool input schemas sent to models come from `z.toJSONSchema(Schema)`. One source, no drift.
- Types prove what the compiler can see. Schemas prove what crosses a trust boundary. Use both.

## Trust boundaries (parse, never cast)

Data is `unknown` until parsed. Call `Schema.parse` (or `safeParse` when failure is expected) at every boundary:

1. Model output and tool-call arguments
2. Events read back from the log (storage is a boundary)
3. Sandbox, network, and file responses
4. Config, env, and user input

Inside the boundary, trust the types. Do not re-validate internally.

## Type rules

- No `any`. No `as` casts except `as const`. No non-null `!`. No `@ts-ignore`; `@ts-expect-error` only in tests, with a reason.
- Discriminated unions for every variant type (the `Event` union is keyed on `t`). Every `switch` over a union ends with `assertNever`.
- Brand identifiers: `ThreadId`, `EventId`, `CallId`, `SandboxId` are distinct branded types (`z.string().brand<"ThreadId">()`). Never pass a raw `string` where an id is meant.
- `readonly` by default. Events are immutable once appended.
- Use `satisfies` to check literals against a type without widening.
- Exported functions declare explicit return types.
- Expected failures are values (`{ ok: true, value } | { ok: false, error }`). `throw` only for bugs and broken invariants.

## Compiler and tooling

`tsconfig.json` must keep all of these on:

```jsonc
"strict": true,
"noUncheckedIndexedAccess": true,
"exactOptionalPropertyTypes": true,
"noImplicitOverride": true,
"noImplicitReturns": true,
"noFallthroughCasesInSwitch": true,
"noPropertyAccessFromIndexSignature": true,
"useUnknownInCatchVariables": true,
"verbatimModuleSyntax": true,
"isolatedDeclarations": true
```

- Runtime and tests: Bun (`bun test`). ESM only.
- Lint and format: Biome, zero warnings.
- CI gate: `tsc --noEmit`, `biome check`, `bun test`. All must pass before merge.

## Code standard

- 2026 JavaScript: `toSorted`/`toSpliced`/`with`, `structuredClone`, `Object.groupBy`, `Promise.withResolvers`, Set methods, iterator helpers. async/await only, no `.then` chains, no `var`, no CommonJS.
- One job per module. Over ~300 lines is a smell; split it.
- Search before writing a helper. One implementation per concern.
- No new dependency without a written reason in the PR. Current allowed runtime dependency: `zod`.
- Comments explain why, not what.

## Framework invariants (never break)

1. The event log is append-only and the only source of truth. State is `reduce(log)`.
2. Side effects never silently repeat: `effect` begin/commit with an idempotency key. Exactly-once holds only where a verified adapter contract allows it: provider dedup within the key's valid window, or reconciliation that returns `confirmed_success | safe_to_retry | unknown`. `unknown` stays parked for a human; a human retry is recorded as accepting duplicate risk.
3. Credentials never enter the sandbox.
4. The prompt prefix stays byte-identical across turns. A test fails on any prefix change.
5. Recalled memory is injected as untrusted reference, never as instructions.
6. The agent cannot write its own config or skills.
7. Event-log style only. No graph/node/edge abstractions.

## Tests

- Every non-trivial function gets a test. Every trust boundary gets a test with invalid input.
- Bug fix = failing test first, then the fix.
- Replay tests: a recorded log must reduce to the same state after any change.
