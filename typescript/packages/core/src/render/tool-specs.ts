import type { EventOf } from "../fold/state";
import {
  type ArtifactRef,
  canonicalize,
  type Json,
  JsonValue,
  type KnownEvent,
  parseStrictJson,
  ToolSpec,
} from "../log";
import { err, ok, type Result } from "../result";
import { sameStub } from "../validate/tool-set";
import { type LogError, logError } from "../verify/error";
import { type ReadRef, readText } from "./lines";
import { toolLine } from "./prefix";

// A deferred tool's full spec lives in its spec_ref artifact (spec/schema/README.md, "Deferred
// tools and tool_search"). Render reads it for a tools_loaded line; import checks it (rule 46,
// rule 17 point 6).

/** The full spec an artifact holds; bytes that are not a ToolSpec are corrupt. */
export function readSpec(
  read: ReadRef,
  ref: ArtifactRef,
  seq: number,
): Result<ToolSpec, LogError> {
  const text = readText(read, ref, seq);
  if (!text.ok) return text;
  const json = parseStrictJson(text.value);
  const spec = json.ok ? ToolSpec.safeParse(json.value) : undefined;
  return spec?.success === true
    ? ok(spec.data)
    : err(
        logError(
          "artifact_corrupt",
          `artifact ${ref.sha256} is not a tool spec`,
          seq,
        ),
      );
}

/** `{"role":"tools_loaded","tools":[…]}`: each loaded spec as line 0 shows a tool. */
export function loadedLine(
  read: ReadRef,
  e: EventOf<"tools_loaded">,
): Result<Json, LogError> {
  const tools: Json[] = [];
  for (const t of e.data.tools) {
    const spec = readSpec(read, t.spec_ref, e.seq);
    if (!spec.ok) return spec;
    tools.push(toolLine(spec.value));
  }
  return ok({ role: "tools_loaded", tools });
}

/**
 * The artifact checks of rule 46 and rule 17 point 6, for one event: a loaded spec is a full
 * form agreeing with its stub, and a full form in a tools_changed is its artifact byte for byte.
 */
export function checkSpecs(
  pinned: ReadonlyMap<string, ToolSpec>,
  e: KnownEvent,
  read: ReadRef,
): Result<void, LogError> {
  if (e.type === "tools_loaded") return checkLoaded(pinned, e, read);
  if (e.type !== "tools_changed") return ok(undefined);
  for (const spec of e.data.tools) {
    const ref = pinned.get(spec.name)?.spec_ref;
    if (ref === undefined || spec.spec_ref !== undefined) continue;
    if (spec.defer_loading === true) continue;
    const bytes = readText(read, ref, e.seq);
    if (!bytes.ok) return bytes;
    const json = JsonValue.safeParse(spec);
    const text = json.success ? canonicalize(json.data) : undefined;
    if (text?.ok !== true || text.value !== bytes.value)
      return invalid(`${spec.name}'s full form is not its spec artifact`, e);
  }
  return ok(undefined);
}

function checkLoaded(
  pinned: ReadonlyMap<string, ToolSpec>,
  e: EventOf<"tools_loaded">,
  read: ReadRef,
): Result<void, LogError> {
  for (const t of e.data.tools) {
    const spec = readSpec(read, t.spec_ref, e.seq);
    if (!spec.ok) return spec;
    const full = spec.value;
    const stub = pinned.get(t.name);
    if (full.defer_loading !== undefined || full.spec_ref !== undefined)
      return invalid(`${t.name}'s spec artifact is still deferred`, e);
    if (stub === undefined || !sameStub(stub, full))
      return invalid(`${t.name}'s spec artifact disagrees with its stub`, e);
  }
  return ok(undefined);
}

function invalid(
  message: string,
  e: KnownEvent,
): { readonly ok: false; readonly error: LogError } {
  return err(logError("invalid_transition", message, e.seq));
}

/** The specs thread_started pinned, by name. */
export function pinnedSpecs(
  events: readonly KnownEvent[],
): ReadonlyMap<string, ToolSpec> {
  const started = events.find((e) => e.type === "thread_started");
  return new Map(
    started?.type === "thread_started"
      ? started.data.tools.map((t) => [t.name, t])
      : [],
  );
}
