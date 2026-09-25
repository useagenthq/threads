import { LEASE_TTL_MS } from "../../store";
import { uuidv7 } from "../../store/encode";
import { TEAM_CONSTANTS } from "../../team/constants";
import type { DynamicChoice } from "../../team/dynamic";
import { materialize, type Rebind } from "../../team/materialize";
import type { MemberRow } from "../../team/rows";
import { memberEntry } from "../registry";
import { pinnedOrLater } from "./scan";
import type { WorkerEnv } from "./worker";

// Rebinding a member's definition by name in this process (design §4.10, prework; spec/schema/
// README.md, "Teams", A failed rebind): a dynamic member's with its recorded choice, never
// re-resolved. Only a definition not registered here (pin_unavailable) or a different config_hash
// (pin_mismatch) fails it. An error setting the definition up is for now ("later"), until it has
// failed Setup attempts times in a row, when the member ends setup_failed.

/**
 * The member's rebind. `setups` counts each member's setup failures in a row, by thread; any
 * other outcome resets its count.
 */
export async function rebindMember(
  env: WorkerEnv,
  setups: Map<string, number>,
  thread: string,
  pin: {
    readonly agent: string;
    readonly configHash: string;
    readonly choice: DynamicChoice | undefined;
  },
): Promise<Rebind | "later"> {
  const handle = env.agents.get(pin.agent);
  const entry = handle === undefined ? undefined : memberEntry(handle);
  if (entry === undefined) return { status: "pin_unavailable" };
  const pinned = await pinnedOrLater(() =>
    entry.pinned(env.deferTools, pin.choice),
  );
  const failed = pinned === "later" ? (setups.get(thread) ?? 0) + 1 : 0;
  setups.set(thread, failed);
  if (failed >= (env.setupAttempts ?? TEAM_CONSTANTS.setupAttempts))
    return { status: "setup_failed" };
  if (pinned === "later") return "later";
  if (pinned === "unbound") return { status: "pin_unavailable" };
  if (pinned.configHash !== pin.configHash) return { status: "pin_mismatch" };
  if (entry.team === undefined) return { status: "ok" };
  const now = env.log.now();
  return {
    status: "ok",
    team: {
      id: uuidv7(now),
      log_thread_id: uuidv7(now),
      log_branch_id: uuidv7(now),
    },
  };
}

/** A member's setup failed for now: materialize stops, and the member is tried again later. */
class Later extends Error {}

/** The member's branch opened by materialize, or "later" when its setup failed for now. */
export async function materializeOrLater(
  env: WorkerEnv,
  row: MemberRow,
  holder: string,
  rebind: (
    agent: string,
    configHash: string,
    choice: DynamicChoice | undefined,
  ) => Promise<Rebind | "later">,
): Promise<Awaited<ReturnType<typeof materialize>> | "later"> {
  try {
    return await materialize(env.log, row.team_id, row.name, {
      artifacts: env.artifacts,
      rebind: async (agent, configHash, choice) => {
        const got = await rebind(agent, configHash, choice);
        if (got === "later") throw new Later();
        return got;
      },
      holder,
      ttlMs: LEASE_TTL_MS,
      ...(env.mint === undefined ? {} : { mint: env.mint }),
    });
  } catch (error) {
    if (error instanceof Later) return "later";
    throw error;
  }
}
