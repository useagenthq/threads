import { type ParkAddress, sameAddress } from "../fold/state";
import type { KnownEvent, MailEnvelope } from "../log";
import type { EventDraft } from "../store/admit";
import type { SqliteDriver } from "../store/driver";
import type { Chain } from "../verify";
import type { Batch } from "./batch";
import { askRow } from "./rows";

// What one team append sees of its own log while it is being built: the committed events plus
// the batch's drafts. The index moves only at commit, so a read later in the same batch must
// skip what the batch already took, closed, finished or resumed (the reference re-reads its log
// after every event it adds: spec/tools/fixtures/ops_world.py).

export type Item = KnownEvent | EventDraft;

/** The committed events, then the batch's drafts (each with its minted event_id). */
export function itemsOf(chain: Chain, batch: Batch): readonly Item[] {
  const committed = chain.events.flatMap((l) =>
    l.kind === "event" ? [l.event] : [],
  );
  return [...committed, ...batch.drafts];
}

/** The parks open once the batch commits: the fold's, less those it resumes, plus its own. */
export function parksNow(chain: Chain, batch: Batch): readonly ParkAddress[] {
  let parks: readonly ParkAddress[] = chain.fold.parked;
  for (const d of batch.drafts)
    if (d.type === "resumed")
      parks = parks.filter((p) => !sameAddress(p, d.data.address));
    else if (d.type === "parked") parks = [...parks, d.data.address];
  return parks;
}

export const parkedOn = (
  chain: Chain,
  batch: Batch,
  address: ParkAddress,
): boolean => parksNow(chain, batch).some((p) => sameAddress(p, address));

/** This log's waits still open once the batch commits. */
export function openWaits(chain: Chain, batch: Batch): ReadonlySet<string> {
  const open = new Set(chain.fold.team.waits);
  for (const d of batch.drafts)
    if (d.type === "wait_started") open.add(d.data.wait_id);
    else if (d.type === "wait_finished") open.delete(d.data.wait_id);
  return open;
}

/** Every settle MonitorId this log registered (the batch's too), mapped to its WaitId. */
export function settleMonitors(
  chain: Chain,
  batch: Batch,
  branch: string,
): ReadonlyMap<string, string> {
  const out = new Map<string, string>();
  for (const e of itemsOf(chain, batch))
    if (e.type === "wait_started")
      for (const m of e.data.members)
        out.set(`${branch}:${e.event_id ?? ""}:${m.name}`, e.data.wait_id);
  return out;
}

/** The ask is this branch's and open: its row says so and the batch hasn't closed it. */
export function askOpen(
  db: SqliteDriver,
  branch: string,
  askId: string,
  batch: Batch,
): boolean {
  const closed = batch.drafts.some(
    (d) => d.type === "ask_closed" && d.data.ask_id === askId,
  );
  const row = askRow(db, askId);
  return !closed && row?.state === "open" && row.asker_branch_id === branch;
}

/** Pending mail the batch hasn't taken yet. */
export function untaken(
  pending: readonly MailEnvelope[],
  batch: Batch,
): readonly MailEnvelope[] {
  const taken = batch.taken();
  return pending.filter((m) => !taken.has(m.mail_id));
}
