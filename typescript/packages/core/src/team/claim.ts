import { z } from "zod";
import type { SqliteDriver } from "../store/driver";
import { TEAM_CONSTANTS } from "./constants";

const Changed = z.array(z.strictObject({ n: z.int() }));

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
): "claimed" | "busy" {
  return db.transaction(() => {
    db.run(
      `UPDATE mail SET claim_token = ?, claim_expires_at = ?
        WHERE mail_id = ? AND state = 'pending'
          AND (claim_token IS NULL OR claim_expires_at <= ?)`,
      [token, now + ttlMs, mailId, now],
    );
    const [row] = Changed.parse(db.all("SELECT changes() AS n", []));
    return row?.n === 1 ? "claimed" : "busy";
  });
}
