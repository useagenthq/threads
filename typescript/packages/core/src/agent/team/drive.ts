import { TEAM_CONSTANTS } from "../../team/constants";
import { type HandleEnv, leadOf } from "./operator-request";
import { agentsOf, memberEntries } from "./runtime";
import { TeamWorker } from "./worker";

// team.ask and team.wait wait for the team log's outcome (spec/schema/README.md, "Teams", the
// operator's handle): until it is there, the handle drives the team's worker as a lead's run
// does, so its members materialize and run, and the team log takes its mail and runs its deadline
// step. The worker stops with the outcome.

/**
 * `outcome()` once it has one, driving the team meanwhile. Throws a member run's bug. It has no
 * ceiling of its own: the outcome comes from the team log's deadline step, which only the lease
 * holder runs, so a lease held elsewhere leaves this call waiting on that holder.
 */
export async function drive<T>(
  env: HandleEnv,
  outcome: () => Promise<T | undefined>,
): Promise<T> {
  const first = await outcome();
  if (first !== undefined) return first;
  // A host team is leadless: its members rebind against the host's registry, and there is no
  // lead whose defer_tools they inherit.
  const lead = env.lead;
  const deferTools =
    lead === undefined
      ? undefined
      : (await leadOf(env.log, env.ref.id)).deferTools;
  const worker = new TeamWorker({
    store: env.store,
    log: env.log,
    artifacts: env.artifacts,
    team: env.ref.id,
    agents: env.agents ?? memberEntries(agentsOf(lead?.team ?? [])),
    ...(env.mint === undefined ? {} : { mint: env.mint }),
    ...(deferTools === undefined ? {} : { deferTools }),
  });
  worker.start();
  try {
    for (;;) {
      // Taken before the read, so progress made after it still wakes this loop.
      const progress = worker.progress();
      const got = await outcome();
      if (got !== undefined) return got;
      await tick(progress);
    }
  } finally {
    await worker.stop();
  }
}

/** The worker's next progress, or the in-process poll: the team log's step runs on each pass. */
async function tick(progress: Promise<void>): Promise<void> {
  const poll = Promise.withResolvers<void>();
  const timer = setTimeout(poll.resolve, TEAM_CONSTANTS.wakePollInProcessMs);
  try {
    await Promise.race([progress, poll.promise]);
  } finally {
    clearTimeout(timer);
  }
}
