// The in-process wake behind team.events({follow}) (spec/api.json Team.events.follow): a commit
// to any of a team's branches wakes this process's followers at once, so a follower polls only
// for the commits of other processes. It carries no data and is never a source of truth: a
// missed wake costs one poll interval, never an item.

/** Waiting followers by team id; each entry is resolved once, by the next commit. */
const waiting = new Map<string, Set<() => void>>();

/** Called after an append to `team` has committed. */
export function teamAppended(team: string): void {
  const woken = waiting.get(team);
  if (woken === undefined) return;
  waiting.delete(team);
  for (const resolve of woken) resolve();
}

/**
 * Registered before the reader looks for new rows, so a commit between the look and the wait
 * still wakes it. `done` drops the registration when the reader stops waiting.
 */
export function teamChanged(team: string): {
  readonly woken: Promise<void>;
  readonly done: () => void;
} {
  const { promise, resolve } = Promise.withResolvers<void>();
  const set = waiting.get(team) ?? new Set<() => void>();
  set.add(resolve);
  waiting.set(team, set);
  return {
    woken: promise,
    done: () => {
      const live = waiting.get(team);
      if (live === undefined) return;
      live.delete(resolve);
      if (live.size === 0) waiting.delete(team);
    },
  };
}
