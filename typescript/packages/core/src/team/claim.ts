import { z } from "zod";
import type { SqliteDriver } from "../store/driver";
import { TEAM_CONSTANTS } from "./constants";

/**
 * What a claim found: `claimed`, or why not. The spec's outcome for each of the others is `busy`
 * (leave the row); they are told apart for the worker's log.
 * - `taken`: another worker's claim on the pending row is still live
 * - `not_pending`: the row was consumed, refused or returned
 * - `not_found`: no mail row has the id
 */
export type Claim = "claimed" | "taken" | "not_pending" | "not_found";

const Changed = z.array(z.strictObject({ n: z.int() }));
const State = z.array(z.strictObject({ state: z.string() }));

/**
 * `mail.claim` (Gate 1 §4.6): marks a pending mail row as this worker's to wake its recipient
 * for, unless another worker's claim on it is still live. A claim only deduplicates wakes: the
 * lease holder consumes, so correctness never depends on it, and it writes no log. An expired
 * claim is taken over by the same statement, so a killed worker's row is woken once after `ttlMs`.
 */
export function claimMail(
  db: SqliteDriver,
  mailId: string,
  token: string,
  now: number,
  ttlMs: number = TEAM_CONSTANTS.claimTtlMs,
): Claim {
  return db.transaction(() => {
    db.run(
      `UPDATE mail SET claim_token = ?, claim_expires_at = ?
        WHERE mail_id = ? AND state = 'pending'
          AND (claim_token IS NULL OR claim_expires_at <= ?)`,
      [token, now + ttlMs, mailId, now],
    );
    const [changed] = Changed.parse(db.all("SELECT changes() AS n", []));
    if (changed?.n === 1) return "claimed";
    const [row] = State.parse(
      db.all("SELECT state FROM mail WHERE mail_id = ?", [mailId]),
    );
    if (row === undefined) return "not_found";
    return row.state === "pending" ? "taken" : "not_pending";
  });
}
