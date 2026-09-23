-- The normative SQLite schema of the log store.
--
-- Both implementations embed this file byte for byte: spec/tools/gen_store_sql.py writes the
-- constants, and CI runs it with --check. Change this file, then regenerate.
--
-- Connection settings, set by each driver before this script runs:
--   PRAGMA journal_mode = WAL;
--   PRAGMA synchronous = FULL;
--   PRAGMA foreign_keys = ON;
--   on darwin, where plain fsync doesn't flush the drive cache:
--   PRAGMA fullfsync = ON;
--   PRAGMA checkpoint_fullfsync = ON;
--
-- user_version is the schema version. A store refuses a database whose user_version is newer
-- than the one it embeds (unsupported_format), so a future change is detectable.

CREATE TABLE IF NOT EXISTS threads (
  thread_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  UNIQUE (thread_id, tenant_id)
) STRICT;

-- head_seq and head_hash are the committed head checkpoint, moved in every append's
-- transaction. head_verified is 0 after an import whose head checkpoint was missing or whose
-- tail was torn; dropped_ref is then the sha256 of the torn bytes' artifact, if any. Such a
-- branch stays inspection_only until recovery appends log_repaired, and its export ends with
-- those bytes instead of a head line. A branch stores only its own rows; parent rows are
-- referenced through parent_branch_id and fork_at_seq, never copied.
CREATE TABLE IF NOT EXISTS branches (
  branch_id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  parent_branch_id TEXT REFERENCES branches (branch_id),
  fork_at_seq INTEGER,
  header_line BLOB NOT NULL,
  state TEXT NOT NULL CHECK (
    state IN ('forking', 'ready', 'inspection_only', 'read_only', 'corrupt', 'fork_failed')
  ),
  head_seq INTEGER NOT NULL,
  head_hash TEXT NOT NULL,
  head_verified INTEGER NOT NULL CHECK (head_verified IN (0, 1)),
  dropped_ref TEXT,
  FOREIGN KEY (thread_id, tenant_id) REFERENCES threads (thread_id, tenant_id)
) STRICT;

-- line is the event's exact bytes. UNIQUE (branch_id, event_id) is physical only: the
-- resolved-chain rule (semantic rule 28) is checked by validate_next.
CREATE TABLE IF NOT EXISTS events (
  branch_id TEXT NOT NULL REFERENCES branches (branch_id),
  seq INTEGER NOT NULL,
  event_id TEXT NOT NULL,
  type TEXT NOT NULL,
  type_version INTEGER NOT NULL,
  critical INTEGER NOT NULL,
  epoch INTEGER NOT NULL,
  line BLOB NOT NULL,
  PRIMARY KEY (branch_id, seq),
  UNIQUE (branch_id, event_id)
) STRICT;

CREATE TABLE IF NOT EXISTS leases (
  branch_id TEXT PRIMARY KEY REFERENCES branches (branch_id),
  holder_id TEXT NOT NULL,
  epoch INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
) STRICT;

-- The cleanup ledger. It is not a projection and outlives the log rows of its owner, so
-- owner_branch_id has no foreign key. operation_key is written with the pending row before
-- the provider call; ref is known once the row is live. expires_at is the provider's own
-- expiry, when the adapter declares one. cleanup_claim is gc's claim token, set by
-- compare-and-set on a collectable row (releasing, release_failed, or live past expires_at);
-- a gc run dispatches a release only while the row still carries its own claim.
CREATE TABLE IF NOT EXISTS resources (
  resource_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  owner_branch_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  kind TEXT NOT NULL,
  ref TEXT,
  state TEXT NOT NULL CHECK (
    state IN ('pending', 'live', 'releasing', 'released', 'release_failed', 'unknown')
  ),
  operation_key TEXT NOT NULL UNIQUE,
  acquired_at INTEGER NOT NULL,
  expires_at INTEGER,
  released_at INTEGER,
  release_outcome TEXT,
  cleanup_claim TEXT
) STRICT;

-- Durable observer cursors: the last seq an observer handled on a branch. An
-- observer resumes after it, so it sees every committed event at least once, in order. It is a
-- projection's bookkeeping, never log state.
CREATE TABLE IF NOT EXISTS observer_cursors (
  observer TEXT NOT NULL,
  branch_id TEXT NOT NULL REFERENCES branches (branch_id),
  seq INTEGER NOT NULL,
  PRIMARY KEY (observer, branch_id)
) STRICT;

-- Tree-wide budget reservations. Before every model_request in any thread of an
-- agent tree, its bound is reserved against every budget covering that thread (its own thread and
-- run budgets and each ancestor's), in one transaction that sums each budget's rows and inserts
-- one row per (budget, limit) only if all fit. The request is appended only after it commits.
-- attempt_key is '<branch_id>:<seq>' of that model_request. A response settles its rows to the
-- attempt's disposition; an attempt not proven unbilled keeps its bound. A
-- reserved row whose attempt never reached the log is released by its branch's next writer.
-- budget_id: 'thread:<thread_id>' (policy.budget) or 'run:<thread_id>:<user_input event_id>'.
-- A projection with a durable cache: the rows can be rebuilt from the tree's logs.
CREATE TABLE IF NOT EXISTS budget_ledger (
  budget_id TEXT NOT NULL,
  limit_name TEXT NOT NULL CHECK (
    limit_name IN ('max_cost_nanos', 'max_input_tokens', 'max_output_tokens', 'max_model_requests')
  ),
  attempt_key TEXT NOT NULL,
  amount INTEGER NOT NULL CHECK (amount >= 0),
  state TEXT NOT NULL CHECK (state IN ('reserved', 'settled')),
  PRIMARY KEY (budget_id, limit_name, attempt_key)
) STRICT;

-- Host-issued memory and knowledge bindings. At every memory write or
-- knowledge ingest the host issues (namespace, record_id) and records which scope owns it; the
-- provider stores the binding opaquely. A returned item is used only when its binding is a row
-- here for the calling (tenant_id, agent, scope). A provider's own scope labels prove nothing.
CREATE TABLE IF NOT EXISTS memory_bindings (
  namespace TEXT NOT NULL,
  record_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  agent TEXT NOT NULL,
  scope TEXT NOT NULL,
  PRIMARY KEY (namespace, record_id)
) STRICT;

CREATE TABLE IF NOT EXISTS knowledge_bindings (
  namespace TEXT NOT NULL,
  record_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  agent TEXT NOT NULL,
  scope TEXT NOT NULL,
  PRIMARY KEY (namespace, record_id)
) STRICT;

-- The host's audit of provider misbehavior: an item with an unknown or foreign binding was
-- dropped, never injected. Audit only: the log never depends on it.
CREATE TABLE IF NOT EXISTS provider_audit (
  audit_id INTEGER PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('memory', 'knowledge')),
  code TEXT NOT NULL CHECK (code IN ('scope_violation')),
  namespace TEXT NOT NULL,
  record_id TEXT NOT NULL,
  at INTEGER NOT NULL
) STRICT;

-- Channel intake. Every item of a verified batch is inserted in one
-- transaction before the webhook response is returned; a redelivered item is a no-op under the
-- UNIQUE key and is still answered. item_key is the provider's per-item id, or
-- '<delivery_id>#<index>' for a batch without one. item is the parsed Inbound item as canonical
-- JSON. thread_id is the conversation's thread (channel_threads). consumed_seq is null until the
-- run appends the item's channel_delivery (or, for a decision or control item, its event) under
-- the branch lease; it is set in that append's transaction.
CREATE TABLE IF NOT EXISTS inbox (
  inbox_id INTEGER PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  channel TEXT NOT NULL,
  installation_id TEXT NOT NULL,
  item_key TEXT NOT NULL,
  delivery_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  item BLOB NOT NULL,
  received_at INTEGER NOT NULL,
  consumed_seq INTEGER,
  UNIQUE (tenant_id, channel, installation_id, item_key)
) STRICT;

-- A channel conversation's thread: the verified (tenant, channel,
-- installation, address) maps to one thread by an atomic create-or-get. Message text or metadata
-- never selects the thread or tenant. A handoff moves the row to the target thread (
-- item 2) with one conditional update.
CREATE TABLE IF NOT EXISTS channel_threads (
  tenant_id TEXT NOT NULL,
  channel TEXT NOT NULL,
  installation_id TEXT NOT NULL,
  address TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  PRIMARY KEY (tenant_id, channel, installation_id, address)
) STRICT;

-- Approval challenges. One single-use row per approval_requested, inserted in
-- that event's append transaction and bound to the tenant, the workspace or installation (null
-- outside a channel), thread, branch, call, canonical-args hash, the hashes of the files the call
-- references (a canonical JSON array) and the expiry. An answer consumes it with one conditional
-- UPDATE ... WHERE state = 'open' in the transaction that appends approval_granted or
-- approval_denied; a second answer finds no open row (approval_duplicate), and an answer at or
-- after expires_at moves it to 'expired' (approval_expired, a denial). decided_by is the
-- approver's PrincipalKey.
CREATE TABLE IF NOT EXISTS approvals (
  challenge_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  installation_id TEXT,
  thread_id TEXT NOT NULL,
  branch_id TEXT NOT NULL,
  call_id TEXT NOT NULL,
  args_hash TEXT NOT NULL,
  file_hashes BLOB NOT NULL,
  expires_at INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('open', 'granted', 'denied', 'expired')),
  decided_by TEXT,
  decided_at INTEGER
) STRICT;

-- Schedule occurrences. occurrence_at is the scheduled instant in UTC ms; the
-- pair is the OccurrenceId. A scheduler claims an occurrence by inserting its row before it
-- appends schedule_fired or schedule_skipped, so two schedulers that see the same due occurrence
-- start one run. state is the claim's outcome; thread_id is the thread it ran or was recorded on.
CREATE TABLE IF NOT EXISTS schedule_occurrences (
  tenant_id TEXT NOT NULL,
  schedule_id TEXT NOT NULL,
  occurrence_at INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('fired', 'skipped')),
  reason TEXT CHECK (reason IN ('missed', 'overlap')),
  thread_id TEXT,
  claimed_at INTEGER NOT NULL,
  UNIQUE (schedule_id, occurrence_at)
) STRICT;

-- POST /v1/runs idempotency (openapi.json Idempotency-Key). The receipt is
-- inserted in the transaction that appends the run's user_input, so a lost response replays
-- it. The key is unique per tenant and operation; principal_key (the full normalized
-- issuer/tenant/subject PrincipalKey) and body_hash (sha256 of the request's canonical JSON) are
-- its binding: the same principal and body replay the receipt and start nothing, a different
-- body is idempotency_key_reused, and a different principal is idempotency_key_principal_mismatch
-- and never sees the receipt. run_id is the user_input's event_id.
CREATE TABLE IF NOT EXISTS run_receipts (
  tenant_id TEXT NOT NULL,
  operation TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  principal_key TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  branch_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (tenant_id, operation, idempotency_key)
) STRICT;

-- Deleted threads (threads delete): one transaction removes a thread's log rows
-- and projections, moves its live resources to releasing, and writes this tombstone, which
-- outlives them as the audit of the deletion.
CREATE TABLE IF NOT EXISTS tombstones (
  thread_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  deleted_at INTEGER NOT NULL
) STRICT;

PRAGMA user_version = 1;
