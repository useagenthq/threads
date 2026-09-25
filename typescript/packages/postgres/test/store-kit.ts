import { LogStore } from "../../core/src/store";
import {
  ROOT,
  started,
  T0,
  THREAD,
  turnCompleted,
  unwrap,
  userInput,
} from "../../core/test/store/helpers";
import { pgArtifacts } from "../src/artifacts";
import { type Faults, openPg, type PgDriver } from "../src/driver";
import { freshSchema } from "./kit";

// A Postgres store on a schema of its own, for the drills.

export { ROOT, started, T0, THREAD, turnCompleted, unwrap, userInput };

export type PgFixture = {
  readonly db: PgDriver;
  readonly store: LogStore;
  readonly url: string;
  readonly clock: { now: number };
  /** Another driver (another "machine") on the same database. */
  readonly peer: (faults?: Faults) => Promise<{
    readonly db: PgDriver;
    readonly store: LogStore;
  }>;
  readonly close: () => Promise<void>;
};

export async function pgFixture(faults: Faults = {}): Promise<PgFixture> {
  const { url, drop } = await freshSchema();
  const clock = { now: T0 };
  const now = (): number => clock.now;
  const opened: PgDriver[] = [];
  const open = async (
    f: Faults,
  ): Promise<{ readonly db: PgDriver; readonly store: LogStore }> => {
    const db = openPg(url, f);
    opened.push(db);
    const store = unwrap(await LogStore.open(db, now, pgArtifacts(db, now)));
    return { db, store };
  };
  const { db, store } = await open(faults);
  return {
    db,
    store,
    url,
    clock,
    peer: (f = {}) => open(f),
    close: async () => {
      for (const d of opened) await d.close();
      await drop();
    },
  };
}
