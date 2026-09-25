import { type ArtifactRef, BranchId, type Json } from "../log";
import { refReader } from "../render/verify";
import { err, ok } from "../result";
import { type ArtifactStore, memoryArtifacts } from "../store/artifacts";
import type { ModelContext } from "./protocol";

/** ModelContext.read over an artifact store: verified sha256 and length. */
export function contextReader(
  artifacts: Pick<ArtifactStore, "get">,
): ModelContext["read"] {
  const read = refReader(artifacts);
  return async (ref) => {
    const bytes = await read(ref, 0);
    if (bytes.ok) return bytes;
    const { code, message } = bytes.error;
    return err({
      code: code === "artifact_missing" ? code : "artifact_corrupt",
      message,
    });
  };
}

/** Thrown inside an adapter's request mapper and caught at its edge: a part it can't send. */
export class Unsendable extends Error {
  override readonly name = "Unsendable";
  /** The send error it becomes: an artifact that can't be read is a provider_error. */
  readonly code:
    | "content_unsupported"
    | "continuation_unsupported"
    | "provider_error";

  constructor(code: Unsendable["code"], message: string) {
    super(message);
    this.code = code;
  }
}

/** context.read for a request mapper: an unreadable artifact makes the request unsendable. */
export function readOrRefuse(
  context: ModelContext,
): (ref: ArtifactRef) => Promise<Uint8Array> {
  return async (ref) => {
    const read = await context.read(ref);
    if (!read.ok) throw new Unsendable("provider_error", read.error.message);
    return read.value;
  };
}

const encoder = new TextEncoder();

/** Stores provider JSON (a reasoning or hosted tool block) for a part to name. */
export function putJson(
  context: ModelContext,
  value: Json,
): Promise<ArtifactRef> {
  return context.put(encoder.encode(JSON.stringify(value)), "application/json");
}

/**
 * Test kit for adapter packages: a ModelContext over in-memory artifacts. `live` stands in for
 * the lease; while it answers false, fence() fails as a stale writer's would.
 */
export function memoryContext(live: () => boolean = () => true): ModelContext {
  const artifacts = memoryArtifacts();
  return {
    branchId: BranchId.parse("00000000-0000-4000-8000-000000000001"),
    epoch: 1,
    fence: async () =>
      live()
        ? ok(undefined)
        : err({ code: "stale_epoch", message: "lease lost" }),
    read: contextReader(artifacts),
    put: async (data, mediaType) => ({
      sha256: await artifacts.put(data),
      bytes: data.length,
      media_type: mediaType,
    }),
  };
}
