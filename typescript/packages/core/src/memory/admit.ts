import { readFileSync } from "node:fs";
import { extname } from "node:path";
import { ConfigError } from "../agent/errors";
import { sha256Hex } from "../hash";
import type { HostBindings } from "../store/bindings";
import type { KnowledgeProvider, Scope } from "./protocol";

// Host ingest of localKnowledge({paths}) (only the host ingests, from config
// or its API). Each run admits each path's current bytes: the same bytes are a no-op, changed
// bytes a new version. A path that can't be read or parsed is a setup error naming it, never
// silently skipped.

/** By extension, as Python's mimetypes guesses them; anything else is read as plain text. */
const TYPES: Readonly<Record<string, string>> = {
  ".md": "text/markdown",
  ".txt": "text/plain",
  ".html": "text/html",
  ".htm": "text/html",
  ".csv": "text/csv",
  ".json": "application/json",
  ".xml": "text/xml",
  ".pdf": "application/pdf",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
};

export async function admitPaths(
  provider: KnowledgeProvider,
  paths: readonly string[],
  scope: Scope,
  bindings: HostBindings,
): Promise<void> {
  for (const path of paths) {
    let content: Uint8Array;
    try {
      content = readFileSync(path);
    } catch (error) {
      throw new ConfigError(
        "invalid_config",
        `knowledge path ${path}: ${String(error)}`,
      );
    }
    const key = `${path}@${sha256Hex(content)}`;
    const done = await provider.ingest(
      scope,
      {
        source_id: path,
        media_type: TYPES[extname(path).toLowerCase()] ?? "text/plain",
        content,
        location: path,
        binding: bindings.issue("knowledge", scope, key),
      },
      key,
    );
    if (!done.ok)
      throw new ConfigError(
        "invalid_config",
        `knowledge path ${path}: ${done.error.message}`,
      );
  }
}
