import { BranchId } from "../log";
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
    const bytes = read(ref, 0);
    if (bytes.ok) return bytes;
    const { code, message } = bytes.error;
    return err({
      code: code === "artifact_missing" ? code : "artifact_corrupt",
      message,
    });
  };
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
      sha256: artifacts.put(data),
      bytes: data.length,
      media_type: mediaType,
    }),
  };
}
