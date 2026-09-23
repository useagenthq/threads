This reference is generated from `spec/api.json`, the contract both implementations follow. Members
that exist in the contract but are not built yet are left out; members built in one language only
say so.

## Naming

- Functions and methods are `lowerCamelCase` in TypeScript and `snake_case` in Python: `openThread` and `open_thread`.
- Types have the same `PascalCase` name in both.
- Required inputs are positional. Everything else is one trailing options object in TypeScript and keyword-only arguments in Python, with the same names and defaults.
- Expected failures are values, not exceptions: TypeScript returns `{ ok: true, value }` or `{ ok: false, error: { code, message } }`, Python returns `Ok[T]` or `Err[Failure]`. Only `ConfigError`, for a definition that can't run, is thrown.

## Packages

| Package | TypeScript | Python |
|---|---|---|
$rows

Model, sandbox, channel and memory providers live in their own packages (`@threads/anthropic`, `threads.anthropic`, ...).
See [Models](/docs/agents/models), [Sandboxes](/docs/sandboxes/overview), [Host server](/docs/host/overview) and [Memory](/docs/memory/memory).

## Functions

| TypeScript | Python | What it does |
|---|---|---|
$fns

## Types

$groups

## HTTP API

The host's HTTP API is documented from its OpenAPI file in the [HTTP API reference](/docs/http-api).
