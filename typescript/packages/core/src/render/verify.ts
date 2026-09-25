import type { EventOf } from "../fold/state";
import { sha256Hex } from "../hash";
import type { ArtifactRef, KnownEvent } from "../log";
import { err, ok, type Result } from "../result";
import type { ArtifactStore } from "../store/artifacts";
import { type LogError, logError } from "../verify/error";
import type { ReadRef } from "./lines";
import { line0 } from "./prefix";
import { compactionSide, type Rendered, render, type Side } from "./render";
import { checkSpecs, pinnedSpecs } from "./tool-specs";

type Artifacts = Pick<ArtifactStore, "get">;
type Fetched = Map<string, Result<Uint8Array, LogError>>;

/**
 * An artifact read that names the event carrying the ref when it fails. The store verifies the
 * sha256; the ref's byte length is checked here, since only the ref records it.
 */
function checked(
  ref: ArtifactRef,
  seq: number,
  bytes: Result<Uint8Array, LogError>,
): Result<Uint8Array, LogError> {
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
}

/** One artifact read from the store, checked against its ref. */
export function refReader(
  artifacts: Artifacts,
): (ref: ArtifactRef, seq: number) => Promise<Result<Uint8Array, LogError>> {
  return async (ref, seq) => checked(ref, seq, await artifacts.get(ref.sha256));
}

/**
 * Render and the checks are synchronous and the store is not: `use` runs over a reader that
 * serves what was fetched and records what wasn't, until a run reads nothing new (what a run
 * reads next may depend on the bytes it read). The returned reader serves the fetched artifacts.
 * `fetched` is a cache shared across calls.
 */
export async function prefetch(
  artifacts: Artifacts,
  use: (read: ReadRef) => unknown,
  fetched: Fetched = new Map(),
): Promise<ReadRef> {
  for (;;) {
    const wanted = new Set<string>();
    use((ref, seq) => {
      const got = fetched.get(ref.sha256);
      if (got !== undefined) return checked(ref, seq, got);
      wanted.add(ref.sha256);
      return ok(new Uint8Array(ref.bytes));
    });
    if (wanted.size === 0) break;
    for (const sha256 of wanted)
      fetched.set(sha256, await artifacts.get(sha256));
  }
  return (ref, seq) =>
    checked(
      ref,
      seq,
      fetched.get(ref.sha256) ??
        err(logError("artifact_missing", `no artifact ${ref.sha256}`)),
    );
}

/** Render v1 of `events` over the store's artifacts. */
export async function renderFrom(
  events: readonly KnownEvent[],
  artifacts: Artifacts,
  side?: Side,
): Promise<Result<Rendered, LogError>> {
  const read = await prefetch(artifacts, (r) => render(events, r, side));
  return render(events, read, side);
}

/**
 * Import-time replay of every `model_request` in seq order (spec/conformance/README.md, render
 * step 3); per request, the first failure wins. C7 first: `declared_prefix` equals the line 0
 * of the request's settings epoch, re-derived every time, so a mismatch never becomes a new
 * baseline and a longer prefix that starts with the old one still fails. Then its
 * `request_ref` artifact must exist and verify, and last the request re-renders to its bytes.
 */
// ponytail: each request re-renders from the start, O(requests x events); incremental when logs get long.
export async function verifyRequests(
  events: readonly KnownEvent[],
  artifacts: Artifacts,
): Promise<Result<void, LogError>> {
  const read = await prefetch(artifacts, (r) => verifyWith(events, r));
  return verifyWith(events, read);
}

function verifyWith(
  events: readonly KnownEvent[],
  read: ReadRef,
): Result<void, LogError> {
  const pinned = pinnedSpecs(events);
  for (const [i, e] of events.entries()) {
    // Spec artifacts (rules 17 and 47) are checked in seq order with the requests.
    const specs = checkSpecs(pinned, e, read);
    if (!specs.ok) return specs;
    if (e.type !== "model_request") continue;
    const request = verifyRequest(events.slice(0, i), e, read);
    if (!request.ok) return request;
  }
  return ok(undefined);
}

/** One request: C7, then its request_ref artifact, then its re-rendered bytes. */
function verifyRequest(
  before: readonly KnownEvent[],
  e: EventOf<"model_request">,
  read: ReadRef,
): Result<void, LogError> {
  const side =
    e.data.purpose === "compaction"
      ? compactionSide(before, e.data.cause_event_id)
      : undefined;
  const prefix = new TextEncoder().encode(`${line0(before)}\n`);
  const declared = e.data.declared_prefix;
  if (declared.bytes !== prefix.length || declared.sha256 !== sha256Hex(prefix))
    return err(
      logError(
        "prefix_changed",
        "declared prefix is not line 0 of its settings epoch",
        e.seq,
      ),
    );
  const recorded = read(e.data.request_ref, e.seq);
  if (!recorded.ok) return recorded;
  const rendered = render(before, read, side);
  if (!rendered.ok) return rendered;
  if (e.data.request_ref.sha256 !== sha256Hex(rendered.value.bytes))
    return err(
      logError(
        "request_hash_mismatch",
        "request_ref is not Render v1 of the events before it",
        e.seq,
      ),
    );
  return ok(undefined);
}
