/**
 * Runs one part of a host's background work (a tick step, a schedule's reservation, a thread's
 * decisions) and reports its failure instead of throwing, so one broken agent, row or thread
 * never stops the others.
 */
export async function isolated(
  what: string,
  part: () => Promise<void>,
): Promise<void> {
  try {
    await part();
  } catch (error) {
    console.error(`threads host: ${what} failed`, error);
  }
}
