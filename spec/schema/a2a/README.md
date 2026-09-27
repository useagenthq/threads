# A2A, pinned

The normative definition of the A2A protocol is `specification/a2a.proto` in
[a2aproject/A2A](https://github.com/a2aproject/A2A) ("the single authoritative normative
definition"). It is vendored here byte for byte, and `a2a.proto.sha256` pins its bytes.

| | |
|---|---|
| Specification | A2A 1.0 |
| Tag | `v1.0.1` |
| Commit | `3303592588e388e62e0f69f701af531d2f4e3991` |
| Vendored file | `a2a.proto` |
| sha256 | `e195bf96ab630c69797851970203e1b2b6b19528f2e9803b7d904b91a5104016` |

`spec/tools/check_a2a_pin.py` re-hashes the file and fails on any drift. A protocol bump is a
deliberate change: fetch the new tag, replace the file, update this table, and re-run the
conformance corpus.

The derived JSON Schema is **not** vendored, because the tag does not ship one: at `v1.0.1`,
`specification/json/` holds only a README saying the JSON representation is generated from the
proto. Our own schema subset (`spec/schema/a2a.v1.schema.json`, exported from the Zod source in
`typescript/packages/a2a/src/protocol/`) is the schema every A2A byte crossing our boundary is
parsed with, and `spec/tools/check_a2a_pin.py` also checks it against the vendored proto:
every field we declare must exist in the proto with a compatible JSON name, and every field the
proto marks `REQUIRED` in a message we parse must be required or explicitly listed as tolerated.

## What we use

Operations: `SendMessage`, `SendStreamingMessage`, `GetTask`, `ListTasks`, `CancelTask`,
`SubscribeToTask`. Bindings: JSON-RPC 2.0 and HTTP+JSON. Not used: the push-notification config
operations, `GetExtendedAgentCard`, and the gRPC binding.

## Recorded discrepancy: `SubscribeToTask`'s HTTP method

The proto's `google.api.http` annotation for `SubscribeToTask` is

```proto
get: "/tasks/{id=*}:subscribe"
```

while the specification prose (§3, operation table) lists it under POST. The proto is normative,
so **we send GET**. Inbound we accept **both** GET and POST on `…/tasks/{id}:subscribe`, so a
client written against the prose interoperates.

## Other places the prose and the proto differ, and what we do

- **Content type.** The HTTP+JSON binding prefers `application/a2a+json` (patch #1753). We send
  it and accept both it and `application/json` inbound. The JSON-RPC binding uses
  `application/json`.
- **`A2A-Version`.** Clients MUST send the header; they MAY send it as a query parameter
  instead. We send the header, and read the header first and then the query parameter inbound.
  Both absent means 0.3 by the spec's rule, which we answer `VersionNotSupportedError`.
- **`/{tenant}/…` path variants.** Every HTTP+JSON operation has an `additional_bindings` with a
  `{tenant}` path prefix. We do not serve them, because our card declares no
  `AgentInterface.tenant`. A request that does carry a `tenant` field must have it equal the
  authenticated principal's tenant.

## Error codes

Taken from the pinned specification's §5.4 mapping table, not from memory. The reason is the
`google.rpc.ErrorInfo` reason that names the same error on the HTTP+JSON binding, which the proto
does not carry because it imports no `google/rpc`; it is the error's own name in the
`UPPER_SNAKE_CASE` the binding uses, and `spec/conformance/vectors/a2a.json` pins all four columns
so both languages agree.

| Error | JSON-RPC code | HTTP status | ErrorInfo reason |
|---|---|---|---|
| `JSONParseError` | `-32700` | 400 | `JSON_PARSE` |
| `InvalidRequestError` | `-32600` | 400 | `INVALID_REQUEST` |
| `MethodNotFoundError` | `-32601` | 404 | `METHOD_NOT_FOUND` |
| `InvalidParamsError` | `-32602` | 400 | `INVALID_PARAMS` |
| `InternalError` | `-32603` | 500 | `INTERNAL` |
| `TaskNotFoundError` | `-32001` | 404 | `TASK_NOT_FOUND` |
| `TaskNotCancelableError` | `-32002` | 400 | `TASK_NOT_CANCELABLE` |
| `PushNotificationNotSupportedError` | `-32003` | 400 | `PUSH_NOTIFICATION_NOT_SUPPORTED` |
| `UnsupportedOperationError` | `-32004` | 400 | `UNSUPPORTED_OPERATION` |
| `ContentTypeNotSupportedError` | `-32005` | 400 | `CONTENT_TYPE_NOT_SUPPORTED` |
| `InvalidAgentResponseError` | `-32006` | 500 | `INVALID_AGENT_RESPONSE` |
| `ExtendedAgentCardNotConfiguredError` | `-32007` | 400 | `EXTENDED_AGENT_CARD_NOT_CONFIGURED` |
| `ExtensionSupportRequiredError` | `-32008` | 400 | `EXTENSION_SUPPORT_REQUIRED` |
| `VersionNotSupportedError` | `-32009` | 400 | `VERSION_NOT_SUPPORTED` |

## `google.rpc.ErrorInfo`

The HTTP+JSON binding answers an error as a `google.rpc.Status`, whose `details` **MUST** carry a
`google.rpc.ErrorInfo`; a JSON-RPC error **SHOULD** carry the same ErrorInfo in its `error.data`. The
vendored proto imports no `google/rpc`, so the shape is pinned by our own schema
(`spec/schema/a2a.v1.schema.json`, `$defs.ErrorInfo`) and named in `spec/tools/check_a2a_pin.py` as a
message of another protocol, with that reason.

We implement the asymmetry the binding states: an HTTP+JSON error body with no ErrorInfo we
recognise is refused as `InvalidAgentResponseError` rather than read, while a JSON-RPC error's
ErrorInfo is checked only when it is there. We emit it on both bindings. `domain` is `a2a.dev`,
because `domain` scopes `reason` and two A2A implementations must name an error the same way;
inbound we read any domain and check only the reason.

A 401 is the one status that overrides the table: it is a challenge a client has to see, so a 401
carrying any reason is accepted rather than checked against that error's own status.

## Our extension

`https://threadsai.dev/a2a/ext/idempotent-send/v1`, `params: {window_ms}`: a `SendMessage` that
repeats a `messageId` from the same authenticated caller within `window_ms` returns the original
task and starts nothing. It is not upstreamed; it keeps its versioned threadsai.dev URI until it
has been checked against a second implementation. A peer is trusted to deduplicate **only** when
its pinned card declares this extension. The spec itself says dedup MAY happen, so nothing else
counts.
