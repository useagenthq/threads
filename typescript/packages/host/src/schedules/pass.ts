import type { LogStore, NewThreadPin, StoreDriver } from "@threads/core/host";
import type { HostContext, HostedAgent } from "../context";

/** One scheduler pass over a tenant. */
export type Pass = {
  readonly ctx: HostContext;
  readonly db: StoreDriver;
  readonly log: LogStore;
  readonly tenant: string;
  /** The thread_started an agent pins, set up once per pass however many schedules use it. */
  readonly started: (hosted: HostedAgent) => Promise<NewThreadPin>;
};

export function newPass(
  ctx: HostContext,
  db: StoreDriver,
  log: LogStore,
  tenant: string,
): Pass {
  const pins = new Map<string, Promise<NewThreadPin>>();
  return {
    ctx,
    db,
    log,
    tenant,
    started: (hosted) => {
      const found = pins.get(hosted.key);
      if (found !== undefined) return found;
      const pinned = hosted.runner.started();
      pins.set(hosted.key, pinned);
      return pinned;
    },
  };
}
