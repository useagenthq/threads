# log

The wire schema of a threads log line, authored in Zod 4. It must accept and reject exactly what `spec/schema/events.v1.schema.json` does.

| File | What it holds |
|---|---|
| `primitives.ts`, `ids.ts` | Integers, hashes, names, `JsonValue`, branded ids |
| `common.ts`, `content.ts`, `policy.ts` | Shared shapes, content parts per position, the pinned `Policy` |
| `envelope.ts` | `Header`, `Head`, the envelope fields and the `event()` factory |
| `events/*.ts` | Per-type `data`, grouped by area; `events/index.ts` is the `KnownEvent` union and the pinned `EVENT_TYPES` |
| `line.ts` | `UnknownEvent` and the `LogLine` union that `bun run schema:export` writes |
| `json.ts` | Strict JSON (duplicate keys, non-finite numbers, unsafe integers, lone surrogates) |
| `parse.ts` | `parseLogLine`: one stored line in, a tagged line or a typed error out |

`parseLogLine` checks one line. It does not check anything that needs other lines.

## TODO: semantic rules (`validate_next`)

These rules are in `spec/schema/README.md` under "Semantic rules". JSON Schema can't express them, so they are not in this folder yet. Each one gets a hand-written check with its conformance case.

- [ ] 1 `seq` contiguous along the resolved chain (`seq_mismatch`)
- [ ] 2 `prev_hash` chain per segment and `fork.at_hash` (`prev_hash_mismatch`)
- [ ] 3 head checkpoint equals the last line (`head_mismatch`)
- [ ] 4 event `branch_id` equals its segment header's
- [ ] 5 unknown critical event refuses the log (done per line in `parseLogLine`; the log-level refusal belongs to import)
- [ ] 6 `epoch` non-decreasing
- [ ] 7 `tool_result` needs a pending call; `tool_result_late` needs a deferred placeholder
- [ ] 8 `effect_begin` needs permission or a consumed approval and no cancel barrier; `read_only` writes no effects
- [ ] 9 uniqueness of `call_id`, `item_key`, `occurrence_id`, approval consumption
- [ ] 10 `compacted` edges are step boundaries, ranges don't partly overlap, summary matches its request
- [ ] 11 no `cancelled` while an effect is unsettled
- [ ] 12 `user_input` only when no turn is open
- [ ] 13 approvals match the open challenge (`approval_mismatch`)
- [ ] 14 C7: `declared_prefix` equals the epoch's line 0 (`prefix_changed`)
- [ ] 15 `request_ref` bytes equal Render v1 (`request_hash_mismatch`)
- [ ] 16 snapshot forks are quiescent and unexpired (`no_snapshot_boundary`, `snapshot_expired`)
- [ ] 17 `tools_hash` is the RFC 8785 hash of `tools`
- [ ] 18 no `settings_changed` during an attempt; its model is in `policy.models`
- [ ] 19 `context_edited` names recorded results; redaction spans are valid
- [ ] 20 `output_validated` matches `policy.output`
- [ ] 21 no `model_request` after `budget_exceeded` until new input
- [ ] 22 `agent_finished` once per child
- [ ] 23 team task claims and updates; unique `message_id`
- [ ] 24 unique todo ids
- [ ] 25 `tool_result{origin: answered}` needs an open input park
- [ ] 26 no input or `model_request` after `handoff`
- [ ] 27 `mode_changed.from` is current; `bypass` needs `allow_bypass`

Other limits:

- Integer fields reject fraction or exponent spellings (`1.0`, `1e3`, ). `json.ts` rejects them everywhere, which is exact only while v1 has no float-typed field. A float field would need field-aware parsing.
- Not checked: whether the line is in JCS canonical form. Readers hash stored bytes and never re-serialize, so this belongs to writers and the export check.
