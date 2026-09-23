import type { EventOf } from "../fold/state";
import { sha256Hex } from "../hash";
import type { KnownEvent } from "../log";
import type { EventDraft } from "../store";
import { draft } from "./drafts";
import { context } from "./hooks";
import { contextPolicy } from "./policy";
import type { Session } from "./session";
import type { Halt } from "./types";

// L3 restore, appended in the same batch as `compacted`, in this order: the output style when
// the dropped range holds the latest one, skills from the range, recently touched files, the todo
// list, a heartbeat of running work. after_compact and session_start{compact} injections follow
// in later appends. Everything restored is an event, so the next request renders it and a replay
// reads it back.

type Injected = EventOf<"injected">;

/** Built-in file tools whose `path` names a file the dropped range worked on. */
const FILE_TOOLS: ReadonlySet<string> = new Set(["read", "write", "edit"]);
const utf8 = new TextDecoder("utf-8", { fatal: true });
const encoder = new TextEncoder();

/**
 * What a compaction of `from..to` restores, built before `compacted` is appended so both land in
 * one batch: a crash never leaves a summary without its restore.
 */
export async function restoreDrafts(
  s: Session,
  from: number,
  to: number,
): Promise<readonly EventDraft[]> {
  const dropped = s.events.filter((e) => e.seq >= from && e.seq <= to);
  return [
    ...style(s, from, to),
    ...skills(s, dropped),
    ...(await files(s, dropped)),
    ...todos(s),
    ...heartbeat(s),
  ];
}

/** The context hooks after a compaction's batch. */
export async function restoreHooks(s: Session): Promise<Halt | undefined> {
  // Context hooks feed the next request; a failure is recorded and restores nothing more.
  const after = await context(s, "after_compact", [s.state()]);
  if (after !== undefined && after !== "failed") return after;
  const started = await context(s, "session_start", ["compact"]);
  return started === "failed" ? undefined : started;
}

/** The latest output style, when the range drops it: the thread keeps replying in it. */
function style(s: Session, from: number, to: number): EventDraft[] {
  const latest = s.events.findLast(
    (e): e is Injected =>
      e.type === "injected" && e.data.source === "output_style",
  );
  return latest !== undefined && latest.seq >= from && latest.seq <= to
    ? [draft.injected(latest.data)]
    : [];
}

/** Each skill's latest injection in the range, capped per skill and in total. */
function skills(s: Session, dropped: readonly KnownEvent[]): EventDraft[] {
  const { skill_tokens, skills_total_tokens } = contextPolicy(
    s.fold.policy,
  ).restore;
  const latest = new Map<string, Injected>();
  for (const e of dropped)
    if (e.type === "injected" && e.data.source === "skill")
      latest.set(e.data.origin.id, e);
  let budget = skills_total_tokens * 4;
  const out: EventDraft[] = [];
  for (const e of latest.values()) {
    const text = e.data.text;
    if (text === undefined) {
      out.push(draft.injected(e.data));
      continue;
    }
    const kept = cap(text, Math.min(skill_tokens * 4, budget));
    if (kept === "") break;
    budget -= encoder.encode(kept).length;
    out.push(draft.injected({ ...e.data, text: kept }));
  }
  return out;
}

/** Up to max_files files the range read or wrote, newest first, read again from the sandbox. */
async function files(
  s: Session,
  dropped: readonly KnownEvent[],
): Promise<EventDraft[]> {
  const read = s.config.readFile;
  if (read === undefined) return [];
  const { max_files, file_tokens } = contextPolicy(s.fold.policy).restore;
  const paths: string[] = [];
  for (const e of dropped.toReversed()) {
    if (e.type !== "tool_call" || !FILE_TOOLS.has(e.data.name)) continue;
    const path = e.data.input["path"];
    if (typeof path === "string" && !paths.includes(path)) paths.push(path);
  }
  const out: EventDraft[] = [];
  for (const path of paths.slice(0, max_files)) {
    const bytes = await read(path);
    if (bytes === undefined) continue;
    out.push(attachment(path, bytes, file_tokens * 4));
  }
  return out;
}

function attachment(
  path: string,
  bytes: Uint8Array,
  limit: number,
): EventDraft {
  const origin = { id: path, version: sha256Hex(bytes) };
  let text: string | undefined;
  try {
    text = bytes.length <= limit ? utf8.decode(bytes) : undefined;
  } catch {
    text = undefined;
  }
  return draft.injected({
    source: "attachment",
    trust: "untrusted_reference",
    origin,
    // Too large or not text: the path only, so the model knows to read it again.
    text:
      text ??
      `${path} (${bytes.length} bytes) was in use; read it again if needed.`,
  });
}

function todos(s: Session): EventDraft[] {
  const list = s.fold.todos;
  if (list.length === 0) return [];
  return [
    draft.injected({
      source: "todo",
      trust: "untrusted_reference",
      origin: { id: "todos" },
      text: list.map((t) => `- [${t.status}] ${t.content}`).join("\n"),
    }),
  ];
}

/** Deferred calls still awaiting their late result: work the summary must not forget. */
function heartbeat(s: Session): EventDraft[] {
  const running = [...s.fold.calls]
    .filter(([, c]) => c.deferred && !c.late)
    .map(([id]) => id);
  return running.length === 0 ? [] : [draft.heartbeat(running)];
}

/** The longest prefix of `text` within `limit` UTF-8 bytes, on a character boundary. */
function cap(text: string, limit: number): string {
  if (encoder.encode(text).length <= limit) return text;
  let out = "";
  let size = 0;
  for (const ch of text) {
    size += encoder.encode(ch).length;
    if (size > limit) break;
    out += ch;
  }
  return out;
}
