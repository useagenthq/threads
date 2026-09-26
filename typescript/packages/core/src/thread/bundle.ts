import { z } from "zod";
import { sha256Hex } from "../hash";
import { canonicalize } from "../log";
import { parseStrictJson } from "../log/json";
import { Int, Sha256 } from "../log/primitives";
import type { Lit, Strict } from "../log/zod-types";
import { err, ok, type Result } from "../result";
import { type LogError, logError } from "../verify/error";

// The bundle manifest (spec/schema/bundle.v1.schema.json): what `bundle.json` holds, and the
// marker that says a bundle directory is complete. Exported from Zod; Python's model is
// generated from it. The layout itself is in spec/schema/README.md, "Portable bundles".

/** The manifest's file name. A directory without it is an incomplete bundle, never a bundle. */
export const MANIFEST = "bundle.json";
/** The bundle's log, the same bytes `threads export` writes to stdout. */
export const LOG_FILE = "log.jsonl";
/** The bundle's artifact directory; each file is named by its lowercase hex sha256. */
export const ARTIFACTS_DIR = "artifacts";

export const Bundle: Strict<{
  format: Lit<1>;
  branch_id: z.ZodString;
  log_sha256: typeof Sha256;
  artifacts: z.ZodRecord<typeof Sha256, typeof Int>;
}> = z
  .strictObject({
    format: z.literal(1),
    branch_id: z.string().min(1).describe("The exported leaf branch."),
    log_sha256: Sha256.describe("sha256 of log.jsonl."),
    artifacts: z
      .record(Sha256, Int)
      .describe("Each artifacts/<sha256> file and its length in bytes."),
  })
  .describe(
    "A portable thread bundle's manifest, written last so its presence means the bundle is complete.",
  );
export type Bundle = z.infer<typeof Bundle>;

/** The manifest's bytes: RFC 8785 canonical JSON. */
export function encodeBundle(manifest: Bundle): Uint8Array {
  const text = canonicalize({
    format: manifest.format,
    branch_id: manifest.branch_id,
    log_sha256: manifest.log_sha256,
    artifacts: { ...manifest.artifacts },
  });
  if (!text.ok) throw new Error("a manifest is JSON");
  return new TextEncoder().encode(text.value);
}

/** `bundle.json` read back: a trust boundary, so its bytes are parsed, never trusted. */
export function decodeBundle(bytes: Uint8Array): Result<Bundle, LogError> {
  const json = parseStrictJson(new TextDecoder().decode(bytes));
  if (!json.ok) return err(incomplete("bundle.json is not JSON"));
  const manifest = Bundle.safeParse(json.value);
  return manifest.success
    ? ok(manifest.data)
    : err(
        incomplete(`bundle.json is not a manifest: ${manifest.error.message}`),
      );
}

/** A bundled file against the manifest: the same bytes, or the bundle is not the one exported. */
export function checkFile(
  name: string,
  bytes: Uint8Array,
  sha256: string,
  size?: number,
): Result<void, LogError> {
  if (size !== undefined && bytes.length !== size)
    return err(
      logError(
        "artifact_corrupt",
        `${name} is ${bytes.length} bytes, not ${size}`,
      ),
    );
  return sha256Hex(bytes) === sha256
    ? ok(undefined)
    : err(logError("artifact_corrupt", `${name} fails its hash`));
}

export function incomplete(why: string): LogError {
  return logError("bundle_incomplete", why);
}

/** A file-system failure under a bundle: the caller names the path it was working on. */
export function ioError(error: unknown, path: string): LogError {
  const why = error instanceof Error ? error.message : String(error);
  return logError("io_error", `${path}: ${why}`);
}
