import { agent, type Model, type Store, scriptedModel } from "@threads/core";
import { markTestKit } from "@threads/core/adapter";
import {
  type BranchId,
  knownEvents,
  openStore,
  type Principal,
  storeConnection,
  tenantStore,
  type VerifiedLog,
} from "@threads/core/host";
import { type Host, host, type RunAccepted } from "../src";
import { authenticate, say } from "./kit";

// The API run recovery tests' kit: hosts on one store, a host whose model never answers (as
// good as dead), and reads of a run's branch.

const hosts: Host[] = [];
const release: (() => void)[] = [];

/** After each test: every stalled model answers, then every host stops. */
export async function cleanup(): Promise<void> {
  for (const open of release.splice(0)) open();
  for (const h of hosts.splice(0)) await h.stop();
}

/** `model`, whose requests never answer until the test ends. */
export function stall(
  model: Model = scriptedModel({ responses: [say("late"), say("late")] }),
): Model {
  const { promise, resolve } = Promise.withResolvers<void>();
  release.push(resolve);
  const made: Model = {
    ...model,
    send: async function* (
      ...[request, context, options]: Parameters<Model["send"]>
    ) {
      await promise;
      yield* model.send(request, context, options);
    },
  };
  markTestKit(made);
  return made;
}

/** A host of one "support" agent on `store`, stopped after the test. */
export function serve(store: Store, model: Model, instructions?: string): Host {
  const support = agent({
    name: "support",
    model,
    ...(instructions === undefined ? {} : { instructions }),
  });
  const h = host({ store, authenticate, agents: { support } });
  hosts.push(h);
  return h;
}

export async function started(
  h: Host,
  as: Principal,
  key: string,
): Promise<RunAccepted> {
  const run = await h.startRun(
    { agent: "support", input: "Hello" },
    { principal: as, idempotencyKey: key },
  );
  if (!run.ok) throw new Error(run.error.message);
  return run.value;
}

export async function fold(
  store: Store,
  tenant: string,
  branch: BranchId,
): Promise<VerifiedLog> {
  const { log } = await openStore(tenantStore(store, tenant));
  const read = log.read(branch);
  if (!read.ok) throw new Error(read.error.message);
  return read.value;
}

export async function has(
  store: Store,
  tenant: string,
  branch: BranchId,
  type: string,
): Promise<boolean> {
  return knownEvents(await fold(store, tenant, branch)).some(
    (e) => e.type === type,
  );
}

/** A dead host's lease runs out (its TTL is 30 s): the drills do the same. */
export async function expireLeases(store: Store): Promise<void> {
  const { db } = await storeConnection(store);
  db.run("UPDATE leases SET expires_at = 0", []);
}
