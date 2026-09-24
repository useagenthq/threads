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
-- than the one it embeds (unsupported_format), so a future change is detectable. It also refuses
-- an existing database of an older nonzero version (unsupported_format: "create a new store"):
-- stores are not migrated before v1, so a new version never runs on a partly installed layout.
-- A new, empty database (version 0) is created at this version.

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
-- budget_id: 'thread:<thread_id>' (policy.budget) or 'run:<thread_id>:<user_input event_id>'; a
-- team member's turn charges the run budget of its root request, which for an operator request is
-- 'run:<team log thread_id>:<operator_request event_id>'.
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

-- Schedule occurrences, per tenant: occurrence_at is the scheduled instant in UTC ms. A
-- scheduler reserves a due occurrence by inserting its 'pending' row (the unique key makes one
-- reservation win) with what firing needs frozen from the schedule at that moment: agent (the
-- host agent key), input_json (the input as canonical JSON) and timezone. reason is 'missed' at
-- reservation when the occurrence fell due while no host ran; a pending row whose thread's log
-- shows an open turn is marked 'overlap' without the writer. Under the thread's writer each
-- pending row is decided in occurrence order by one conditional UPDATE ... WHERE state =
-- 'pending' in the transaction that appends its schedule_fired or schedule_skipped, which sets
-- state, reason and logged_seq (the event's seq); a stale scheduler's update matches nothing and
-- its append rolls back. An occurrence whose agent is gone, or now pins another config than its
-- thread's, is skipped as 'removed', never fired into a run its thread's pin would refuse. A
-- thread's deletion turns its pending rows 'retired': never logged, and never an occurrence
-- outcome; the row only keeps the key from being reserved again.
CREATE TABLE IF NOT EXISTS schedule_occurrences (
  tenant_id TEXT NOT NULL,
  schedule_id TEXT NOT NULL,
  occurrence_at INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('pending', 'fired', 'skipped', 'retired')),
  reason TEXT CHECK (reason IN ('missed', 'overlap', 'removed')),
  thread_id TEXT NOT NULL,
  claimed_at INTEGER NOT NULL,
  logged_seq INTEGER,
  agent TEXT,
  input_json TEXT,
  timezone TEXT,
  UNIQUE (tenant_id, schedule_id, occurrence_at),
  CHECK (
    state <> 'pending' OR (agent IS NOT NULL AND input_json IS NOT NULL AND timezone IS NOT NULL)
  )
) STRICT;

-- Every scheduler tick sweeps its tenant's pending rows; this keeps it off decided history.
CREATE INDEX IF NOT EXISTS schedule_occurrences_pending
  ON schedule_occurrences (tenant_id, occurrence_at) WHERE state = 'pending';

-- Every thread a schedule has had; current = 1 marks the one new occurrences go to (one per
-- schedule, by the partial unique index). A schedule keeps its thread while its agent's pinned
-- config is unchanged; a config change moves it to a new one once the old one is quiet. Older
-- threads stay listed, so recovery resumes a run left open on any of them (an input sent there
-- through the run API, say). A scheduler finds the current row, or writes a new one with the
-- thread's branch and thread_started, in the same transaction as the reservations it makes on
-- it, so a concurrent deletion lands wholly before or after. Deleted with its thread.
CREATE TABLE IF NOT EXISTS schedule_threads (
  tenant_id TEXT NOT NULL,
  schedule_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  current INTEGER NOT NULL CHECK (current IN (0, 1)),
  created_at INTEGER NOT NULL,
  PRIMARY KEY (tenant_id, schedule_id, thread_id)
) STRICT;

CREATE UNIQUE INDEX IF NOT EXISTS schedule_threads_current
  ON schedule_threads (tenant_id, schedule_id) WHERE current = 1;

-- ask_user questions: a rebuildable projection of the log. A row is inserted 'open' in the
-- transaction that appends parked{awaiting_input} and changes only when the settling tool_result
-- is appended ('answered' or 'expired'); time alone never changes it.
CREATE TABLE IF NOT EXISTS questions (
  tenant_id TEXT NOT NULL,
  branch_id TEXT NOT NULL,
  call_id TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('open', 'answered', 'expired')),
  decided_at INTEGER,
  PRIMARY KEY (branch_id, call_id)
) STRICT;

CREATE INDEX IF NOT EXISTS questions_due ON questions (state, expires_at);

-- POST /v1/runs idempotency (openapi.json Idempotency-Key). The receipt is
-- inserted in the transaction that appends the run's user_input, so a lost response replays
-- it. The key is unique per tenant and operation; principal_key (the full normalized
-- PrincipalKey: issuer/tenant/subject, each part with % then / escaped as %25 and %2F, the
-- same form in every principal_key column) and body_hash (sha256 of the request's canonical JSON) are
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

-- Teams (spec/schema/README.md, "Teams"; store version 4). Every row below is an index of the
-- logs: it is inserted or changed only in the transaction of an append whose events hold every
-- byte it needs, so wiping these tables and folding the team's logs (every member log and the
-- team log) rebuilds them byte for byte. The one exception is mail's claim columns, which are
-- wake hints and rebuild as null. JSON columns hold RFC 8785 bytes.

-- team_opened (in the team log, written by the lead's first append) inserts the row; the lead's
-- member_ended sets closed_at to that event's time.
CREATE TABLE IF NOT EXISTS teams (
  team_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  lead_thread_id TEXT NOT NULL,
  team_log_branch_id TEXT NOT NULL,
  closed_at INTEGER
) STRICT;

-- A team log belongs to one team; every append asks whether its branch is one.
CREATE UNIQUE INDEX IF NOT EXISTS teams_log_branch ON teams (team_log_branch_id);

-- One row per member generation, the lead included: role lead is inserted by the lead's first
-- append, from its thread_started{team} and the team log's team_opened (a lead has no
-- provenance); every other row comes from member_started. branch_id is null only in the starting
-- window, before materialize opens the member's branch. state and result
-- change only in the member's own appends: a turn opener sets running, parked and resumed set
-- parked and running, member_idle sets idle and result, member_ended sets ended and result.
-- updated_seq is the seq of the event that last wrote the row, in the log that holds it.
CREATE TABLE IF NOT EXISTS team_members (
  team_id TEXT NOT NULL,
  name TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK (generation >= 1),
  role TEXT NOT NULL CHECK (role IN ('lead', 'member')),
  agent TEXT NOT NULL,
  config_hash TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  branch_id TEXT,
  provenance BLOB,
  state TEXT NOT NULL CHECK (state IN ('starting', 'running', 'idle', 'parked', 'ended')),
  result BLOB,
  updated_seq INTEGER NOT NULL,
  PRIMARY KEY (team_id, name, generation),
  CHECK ((state = 'starting') = (branch_id IS NULL)),
  CHECK ((role = 'lead') = (provenance IS NULL))
) STRICT;

-- Every append asks which team a branch belongs to (its team rows and feed), by thread and branch.
CREATE INDEX IF NOT EXISTS team_members_thread ON team_members (thread_id, branch_id);

-- One row per mail, inserted by the sender's message_sent: envelope is that event's envelope
-- byte for byte, and created_at is its time (the consume order is (created_at, mail_id)). The
-- recipient's writer moves it once: message_received (or a task's user_input{source: team_task})
-- to consumed, mail_refused to stale (stale_member) or returned (member_ended). A null to_name is
-- the team log. claim_token and claim_expires_at deduplicate wakes among workers and are never
-- needed for correctness.
CREATE TABLE IF NOT EXISTS mail (
  mail_id TEXT PRIMARY KEY,
  team_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (
    kind IN (
      'message', 'ask', 'reply', 'task', 'cancel',
      'member_settled', 'member_parked', 'member_ended', 'bounce'
    )
  ),
  to_name TEXT,
  to_generation INTEGER,
  principal_key TEXT NOT NULL,
  root_request TEXT NOT NULL,
  envelope BLOB NOT NULL,
  created_at INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('pending', 'consumed', 'stale', 'returned')),
  claim_token TEXT,
  claim_expires_at INTEGER,
  consumed_seq INTEGER,
  CHECK ((to_name IS NULL) = (to_generation IS NULL))
) STRICT;

CREATE INDEX IF NOT EXISTS mail_pending
  ON mail (team_id, to_name, to_generation, created_at, mail_id)
  WHERE state = 'pending';

-- One row per ask, inserted by the ask's message_sent (ask_id = its mail_id); the asker's
-- ask_closed moves it from open to its outcome with one conditional update.
CREATE TABLE IF NOT EXISTS asks (
  ask_id TEXT PRIMARY KEY,
  team_id TEXT NOT NULL,
  asker_branch_id TEXT NOT NULL,
  recipient_name TEXT NOT NULL,
  recipient_generation INTEGER NOT NULL,
  deadline INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (
    state IN ('open', 'answered', 'timed_out', 'member_ended', 'cancelled')
  ),
  closed_seq INTEGER
) STRICT;

-- Live monitors. monitor_id is derived, '<watcher branch_id>:<registering event_id>:<target
-- name or task>'. Inserted by monitor_set (end), wait_started (settle, one per listed member not
-- already settled) or member_started (task). Deleted by the one event that names it: the
-- target's firing message_sent{monitor_id}, or wait_finished{wait_id} for every remaining row of
-- that wait.
CREATE TABLE IF NOT EXISTS monitors (
  monitor_id TEXT PRIMARY KEY,
  team_id TEXT NOT NULL,
  watcher_branch_id TEXT NOT NULL,
  target_name TEXT NOT NULL,
  target_generation INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('end', 'settle', 'task')),
  wait_id TEXT,
  CHECK ((kind = 'settle') = (wait_id IS NOT NULL))
) STRICT;

CREATE INDEX IF NOT EXISTS monitors_target
  ON monitors (team_id, target_name, target_generation);

-- Operator idempotency, per team: inserted with the team log's operator_request that carries an
-- idempotency_key. The binding is as run_receipts': the same principal and body return the
-- request_id's outcome, a different body is idempotency_key_reused, a different principal is
-- idempotency_key_principal_mismatch.
CREATE TABLE IF NOT EXISTS operator_receipts (
  tenant_id TEXT NOT NULL,
  team_id TEXT NOT NULL,
  op TEXT NOT NULL CHECK (op IN ('start', 'send', 'ask', 'wait', 'cancel')),
  idempotency_key TEXT NOT NULL,
  principal_key TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  request_id TEXT NOT NULL,
  PRIMARY KEY (tenant_id, team_id, op, idempotency_key)
) STRICT;

-- The team feed's delivery order: one row per event appended to any of the team's logs,
-- assigned in the append's transaction. A lost index is rebuilt under a new epoch, so a cursor
-- (epoch, feed_offset) from an older epoch restarts.
CREATE TABLE IF NOT EXISTS team_feed (
  team_id TEXT NOT NULL,
  epoch INTEGER NOT NULL,
  feed_offset INTEGER NOT NULL,
  branch_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  PRIMARY KEY (team_id, epoch, feed_offset),
  UNIQUE (team_id, epoch, branch_id, seq)
) STRICT;

-- Legacy background subagents: one row per running background child, inserted by its
-- agent_spawned{mode: background} and deleted by its agent_finished, or by the parent's
-- parked{kind: child} for it. A host takes a branch with rows and a free lease, relaunches those
-- children and records each end with its woken in one append.
CREATE TABLE IF NOT EXISTS pending_wakes (
  branch_id TEXT NOT NULL,
  child_thread_id TEXT NOT NULL,
  PRIMARY KEY (branch_id, child_thread_id)
) STRICT;

-- Telemetry exporters (spec/otel/README.md; store version 6). An exporter registers itself here
-- on its first sync, so a deletion knows whose unsent spans it may drop.
CREATE TABLE IF NOT EXISTS observers (
  name TEXT PRIMARY KEY,
  registered_at INTEGER NOT NULL
) STRICT;

-- What a deletion may have dropped before an exporter sent it: one row per registered observer,
-- inserted in the delete transaction. unchecked_events counts the thread's committed events past
-- that observer's cursor on each of its branches (every event of a branch without a cursor), an
-- upper bound. reported_at is set once the row has been exported. Rows are never deleted: they
-- are the audit of the deletion.
CREATE TABLE IF NOT EXISTS observer_losses (
  observer TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  unchecked_events INTEGER NOT NULL CHECK (unchecked_events >= 0),
  deleted_at INTEGER NOT NULL,
  reported_at INTEGER,
  PRIMARY KEY (observer, thread_id, deleted_at)
) STRICT;

CREATE INDEX IF NOT EXISTS observer_losses_unreported
  ON observer_losses (observer) WHERE reported_at IS NULL;

-- Version 4: the team tables and pending_wakes. Version 5: lane 14C's schedule_threads,
-- tenant-scoped schedule_occurrences with pending and retired rows, and questions (planned as
-- version 2; the team tables took 4 first, so a version-4 store has the older schedule layout and
-- is refused). Version 6: lane 23's observers and observer_losses.
PRAGMA user_version = 6;
