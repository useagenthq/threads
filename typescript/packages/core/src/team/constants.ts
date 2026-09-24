/**
 * The Teams constants (spec/schema/README.md, "Teams", Constants), the same in both runtimes.
 * Each operation that uses one takes it as a parameter defaulting to this value, so tests inject
 * others.
 */
export const TEAM_CONSTANTS = {
  /** A text value larger than this many UTF-8 bytes becomes an artifact ref. */
  inlineCapBytes: 16_384,
  /** How long an operator request retries a held team-log lease before `refused{busy}`. */
  busyBoundMs: 5_000,
  /** How long a `mail.claim` holds a pending row. */
  claimTtlMs: 30_000,
  /** The default deadline of `ask` and `wait`, and their cap. */
  askWaitDefaultMs: 120_000,
  wakePollInProcessMs: 250,
  wakePollCrossProcessMs: 1_000,
} as const;
