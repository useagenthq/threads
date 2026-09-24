# spec/models

Each model factory's catalog: the limits of exact model ids, so `anthropic("claude-sonnet-5")` and `openai("gpt-5.5")` need no numbers. The shape is `../schema/model-catalog.v1.schema.json`; both runtimes embed these files (`../tools/gen_model_catalogs.py`, run by `scripts/generate.sh`).

| File | What it is |
|---|---|
| `<provider>.v1.json` | The entries (`id`, `max_input_tokens`, `max_output_tokens`, optional `alias_of`, `source`, optional `note`, `verified`) and the withdrawals. `provider` equals the file name's first part, and every withdrawal names an entry |
| `<provider>.v1.lock.json` | Per id, the sha256 of the entry's behavioral fields and its last accepted `verified` date; per withdrawal, the sha256 of the whole withdrawal |

## Where the numbers come from

Every entry names its `source` and the day it was read there (`verified`). Values are copied from the provider, never guessed.

- **Anthropic**: the Models API, `GET https://api.anthropic.com/v1/models/<id>`: `max_input_tokens` and `max_tokens` (our `max_output_tokens`). The request costs no tokens.
- **OpenAI**: the model page, `https://developers.openai.com/api/docs/models/<id>`. When the page publishes a maximum input (GPT-6 Sol: 922,000), that is `max_input_tokens`. The GPT-5.5 page publishes a 1,050,000-token context window and 128,000 max output tokens but no maximum input, so its `max_input_tokens` is **derived** as window minus cap, 922,000. That relation is our inference, not a statement on either page: the gpt-6-sol page publishes 922,000 input with the same 1,050,000 window and 128,000 cap. The two gpt-5.5 entries say so in `note`. `gpt-5.5` is an alias entry with `alias_of` its snapshot, `gpt-5.5-2026-04-23` (the page lists it).

An entry's `note` records how a value was obtained when its `source` doesn't state it outright. Like `source` and `verified`, it is evidence: it isn't in the locked behavioral fields.

Verified 2026-09-24 for every entry.

## Changing a catalog

- **Add a model**: append an entry, then `python3 spec/tools/check_model_catalog.py --add` to lock it.
- **Re-verify**: update `source` or move `verified` forward, then `--refresh` so the lock records the new date. A date never moves backwards.
- **A wrong entry is never edited.** Its id, limits and `alias_of` are locked. Add `{id, reason, use}` to `withdrawn` (`use` holds the corrected limits) and run `--add`. The factory then refuses the id without explicit limits and prints both the corrected values and the original ones, which keep a thread started with them continuing. A correct value for a new snapshot ships as a new id.

CI runs `check_model_catalog.py` with no flags: any change to a locked entry or withdrawal, a removed one, an unlocked one, or a date that doesn't match its lock fails.
