import type { Writer } from "./writer";

// The writers of the runs this process is executing, by branch. A control on such a branch
// appends through the run's own writer, so it lands between the run's steps and the run sees it
// at its next one.

const live = new Map<string, Writer>();

/** Registers `writer` as its branch's running writer until the returned function is called. */
export function running(writer: Writer): () => void {
  const branch = writer.lease.branchId;
  live.set(branch, writer);
  return () => {
    if (live.get(branch) === writer) live.delete(branch);
  };
}

export function liveWriter(branch: string): Writer | undefined {
  return live.get(branch);
}
