import type { Writer } from "./writer";

// The writers of the runs this process is executing, by branch. A control on such a branch
// appends through the run's own writer, so it lands between the run's steps and the run sees it
// at its next one (one writer per branch, never a second lease in this process).

const live = new Map<string, Writer>();

/** Registers `writer` as its branch's running writer until the returned function is called. */
function running(writer: Writer): () => void {
  const branch = writer.lease.branchId;
  live.set(branch, writer);
  return () => {
    if (live.get(branch) === writer) live.delete(branch);
  };
}

export function liveWriter(branch: string): Writer | undefined {
  return live.get(branch);
}

/**
 * Holds `writer`'s lease while its work is in flight: registered as its branch's running writer,
 * renewed every third of its TTL so slow model, tool and channel calls keep it, then handed back
 * so the next executor starts at once. A failed renewal poisons the writer, which fences every
 * later dispatch and append.
 */
export function keepLease(writer: Writer): () => void {
  const { ttlMs } = writer.lease;
  const timer = setInterval(() => {
    if (!writer.renew(ttlMs).ok) clearInterval(timer);
  }, ttlMs / 3);
  const done = running(writer);
  return () => {
    clearInterval(timer);
    done();
    writer.release();
  };
}
