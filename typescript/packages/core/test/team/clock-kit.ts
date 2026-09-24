import { type Model, scriptedModel } from "../../src";
import { type Store, storeOf } from "../../src/agent/sqlite";
import { markTestKit } from "../../src/model/guard";
import { LogStore, memoryArtifacts } from "../../src/store";
import { openBunSqlite } from "../../src/store/bun-sqlite";
import { unwrap } from "../store/helpers";

// Shared by the team deadline tests: a store on a clock the test moves, a member held at its
// first request, and polling until a condition holds.

/**
 * A store on a clock the test moves (deadlines are read from the store's clock). Time passing
 * also renews every live lease, as each holder's renewal timer would have.
 */
export function clocked(): {
  readonly store: Store;
  readonly log: LogStore;
  readonly elapse: (ms: number) => void;
} {
  const clock = { now: Date.now() };
  const artifacts = memoryArtifacts();
  const db = openBunSqlite(":memory:");
  const log = unwrap(LogStore.open(db, () => clock.now, artifacts));
  const elapse = (ms: number): void => {
    db.run(
      "UPDATE leases SET expires_at = expires_at + ? WHERE expires_at > ?",
      [ms, clock.now],
    );
    clock.now += ms;
  };
  return { store: storeOf({ log, artifacts }), log, elapse };
}

/** A scripted model whose first request waits for `open`. */
export function held(
  open: Promise<void>,
  responses: readonly unknown[],
): Model {
  const base = scriptedModel({ responses: [...responses] });
  let first = true;
  const made: Model = {
    ...base,
    send: async function* (...args: Parameters<Model["send"]>) {
      if (first) {
        first = false;
        await open;
      }
      yield* base.send(...args);
    },
  };
  markTestKit(made);
  return made;
}

/** Resolves once `check` holds, polling. */
export async function until(check: () => Promise<boolean>): Promise<void> {
  for (let i = 0; i < 500; i += 1) {
    if (await check()) return;
    await new Promise((r) => setTimeout(r, 10));
  }
  throw new Error("timed out waiting");
}
