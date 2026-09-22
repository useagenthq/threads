import { sha256Hex } from "../hash";
import type { ArtifactRef, KnownEvent } from "../log";
import { err, ok, type Result } from "../result";
import type { ArtifactStore } from "../store/artifacts";
import { type LogError, logError } from "../verify/error";
import type { ReadRef } from "./lines";
import { line0 } from "./prefix";
import { compactionInstruction, render } from "./render";

/**
 * Artifact reads that name the event carrying the ref when they fail. The store verifies the
 * sha256; the ref's byte length is checked here, since only the ref records it.
 */
export function refReader(artifacts: Pick<ArtifactStore, "get">): ReadRef {
  return (ref: ArtifactRef, seq: number) => {
    const bytes = artifacts.get(ref.sha256);
    if (!bytes.ok) return err({ ...bytes.error, seq });
    return bytes.value.length === ref.bytes
      ? bytes
      : err(
          logError(
            "artifact_corrupt",
            `artifact ${ref.sha256} is ${bytes.value.length} bytes, its ref says ${ref.bytes}`,
            seq,
          ),
        );
  };
}

/**
 * Import-time replay of every `model_request` in seq order (spec/conformance/README.md, render
 * step 3); per request, the first failure wins. C7 first: `declared_prefix` equals the line 0
 * of the request's settings epoch, re-derived every time, so a mismatch never becomes a new
 * baseline and a longer prefix that starts with the old one still fails. Then its
 * `request_ref` artifact must exist and verify, and last the request re-renders to its bytes.
 */
// ponytail: each request re-renders from the start, O(requests x events); incremental when logs get long.
export function verifyRequests(
  events: readonly KnownEvent[],
  read: ReadRef,
): Result<void, LogError> {
  for (const [i, e] of events.entries()) {
    if (e.type !== "model_request") continue;
    const before = events.slice(0, i);
    const instruction =
      e.data.purpose === "compaction"
        ? compactionInstruction(before)
        : undefined;
    const prefix = new TextEncoder().encode(`${line0(before)}\n`);
    const declared = e.data.declared_prefix;
    if (
      declared.bytes !== prefix.length ||
      declared.sha256 !== sha256Hex(prefix)
    )
      return err(
        logError(
          "prefix_changed",
          "declared prefix is not line 0 of its settings epoch",
          e.seq,
        ),
      );
    const recorded = read(e.data.request_ref, e.seq);
    if (!recorded.ok) return recorded;
    const rendered = render(before, read, instruction);
    if (!rendered.ok) return rendered;
    if (e.data.request_ref.sha256 !== sha256Hex(rendered.value.bytes))
      return err(
        logError(
          "request_hash_mismatch",
          "request_ref is not Render v1 of the events before it",
          e.seq,
        ),
      );
  }
  return ok(undefined);
}
