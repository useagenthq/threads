-- The normative SQLite schema of the log store.
--
-- Both implementations embed this file byte for byte: spec/tools/gen_store_sql.py writes the
-- constants, and CI runs it with --check. Change this file, then regenerate.
--
-- Tables for later features are added with those features: inbox, approvals,
-- schedule_occurrences, knowledge_* and memory_*.
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

PRAGMA user_version = 1;
