import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { openStore, type Store } from "../agent/sqlite";
import { knownEvents } from "../reduce";
import { verifyRequests } from "../render";
import { err, ok, type Result } from "../result";
import type { ArtifactStore } from "../store";
import { verified } from "../store/artifacts";
import { deleted } from "../store/import-rows";
import type { LogStore } from "../store/store";
import { type VerifiedLog, verifyExport } from "../verify";
import type { LogError } from "../verify/error";
import {
  ARTIFACTS_DIR,
  checkFile,
  decodeBundle,
  incomplete,
  ioError,
  LOG_FILE,
  MANIFEST,
} from "./bundle";
import { type Thread, threadHandle } from "./handle";
import { refsIn } from "./save-case";

// importThread() (spec/api.json): a bundle directory or a bare .jsonl export, stored in this
// store. Everything is validated before anything is written; then the artifacts, then all the
// rows in one transaction. A failure after prevalidation can leave artifact files with no rows
// naming them: they are content-addressed, never read without a ref, and reused by a retry.

export async function importThread(
  store: Store,
  path: string,
): Promise<Result<Thread, LogError>> {
  const opened = await openStore(store);
  const read = bundle(path);
  if (!read.ok) return read;
  const checked = await prevalidate(opened.log, opened.artifacts, read.value);
  if (!checked.ok) return checked;
  for (const bytes of read.value.artifacts.values())
    await opened.artifacts.put(bytes);
  const stored = await opened.log.importLog(read.value.log);
  if (!stored.ok) return stored;
  const leaf = stored.value.segments.at(-1)?.header;
  if (leaf === undefined) return err(incomplete(`${path} holds no segment`));
  return ok(
    threadHandle(opened, {
      id: leaf.thread_id,
      branch: leaf.branch_id,
      store,
    }),
  );
}

type Read = {
  readonly log: Uint8Array;
  readonly artifacts: ReadonlyMap<string, Uint8Array>;
};

/** A bundle directory, checked against its manifest, or a bare .jsonl export on its own. */
function bundle(path: string): Result<Read, LogError> {
  let directory = false;
  try {
    directory = statSync(path).isDirectory();
  } catch (error) {
    return err(ioError(error, path));
  }
  try {
    return directory
      ? manifested(path)
      : ok({ log: bytes(path), artifacts: new Map() });
  } catch (error) {
    return err(ioError(error, path));
  }
}

function manifested(path: string): Result<Read, LogError> {
  const present = new Set(readdirSync(path));
  if (!present.has(MANIFEST))
    return err(
      incomplete(`${path} has no ${MANIFEST}; the export never finished`),
    );
  const manifest = decodeBundle(bytes(join(path, MANIFEST)));
  if (!manifest.ok) return manifest;
  if (!present.has(LOG_FILE))
    return err(incomplete(`${path} has no ${LOG_FILE}`));
  const log = bytes(join(path, LOG_FILE));
  const same = checkFile(LOG_FILE, log, manifest.value.log_sha256);
  if (!same.ok) return same;
  const files = new Map<string, Uint8Array>();
  for (const [sha256, size] of Object.entries(manifest.value.artifacts)) {
    const file = join(path, ARTIFACTS_DIR, sha256);
    let body: Uint8Array;
    try {
      body = bytes(file);
    } catch {
      return err(incomplete(`${path} is missing ${ARTIFACTS_DIR}/${sha256}`));
    }
    const ok_ = checkFile(`${ARTIFACTS_DIR}/${sha256}`, body, sha256, size);
    if (!ok_.ok) return ok_;
    files.set(sha256, body);
  }
  return ok({ log, artifacts: files });
}

function bytes(file: string): Uint8Array {
  return new Uint8Array(readFileSync(file));
}

/**
 * The log, its model requests and every artifact it names, checked with nothing written: the
 * bundle's files stand in for artifacts the store doesn't hold yet.
 */
async function prevalidate(
  log: LogStore,
  artifacts: ArtifactStore,
  read: Read,
): Promise<Result<VerifiedLog, LogError>> {
  const verifiedLog = verifyExport(read.log);
  if (!verifiedLog.ok) return verifiedLog;
  const events = knownEvents(verifiedLog.value);
  const overlay = bundled(artifacts, read.artifacts);
  const replayed = await verifyRequests(events, overlay);
  if (!replayed.ok) return replayed;
  const refs = new Set<string>();
  refsIn(events, refs);
  for (const sha256 of refs) {
    const got = await overlay.get(sha256);
    if (!got.ok) return got;
  }
  const gone = await log.deletedThread(
    verifiedLog.value.segments.map((s) => s.header.thread_id),
  );
  if (!gone.ok) return gone;
  if (gone.value !== undefined) return err(deleted(gone.value));
  return verifiedLog;
}

/** The destination's artifacts with the bundle's laid over them; writes go to the destination. */
function bundled(
  artifacts: ArtifactStore,
  files: ReadonlyMap<string, Uint8Array>,
): ArtifactStore {
  return {
    ...artifacts,
    get: async (sha256) => {
      const file = files.get(sha256);
      return file === undefined
        ? await artifacts.get(sha256)
        : verified(sha256, file);
    },
  };
}
