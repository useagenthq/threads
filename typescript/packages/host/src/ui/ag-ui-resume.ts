import {
  assertNever,
  err,
  ok,
  type Principal,
  type Result,
} from "@threads/core/host";
import type { z } from "zod";
import type { HostContext } from "../context";
import { answerQuestion, decideChallenge, resumeThread } from "../decisions";
import type { Failure } from "../errors";
import { Answer, ApprovalDecision } from "../schemas";
import type { AgUiResumeEntry } from "./bodies";
import { type Log, readLog, SETTLED_MEANWHILE } from "./common";
import type { Chunk } from "./frame";
import { type Interrupt, interruptOf, type Recorded } from "./settled";

// An AG-UI input's `resume` entries (spec/schema/ui/README.md, "Bodies"). An entry for an
// interrupt still open is recorded through the same controls as the REST routes. One for an
// interrupt already settled (by the same value, another value, another approver, or expiry)
// records nothing and is never an error: the stock client would be locked out of its thread.
// Where its value differs from the log's, the stream says so with threads.resume_conflict.

type Answerable = Extract<Interrupt, { kind: "approval" | "question" }>;
type Classified = { readonly entry: AgUiResumeEntry; readonly at: Answerable };

/** The conflicts to report, once every open entry is recorded. */
export async function applyResume(
  ctx: HostContext,
  principal: Principal,
  log: Log,
  entries: readonly AgUiResumeEntry[],
  options: { readonly now: number; readonly newMessage: boolean },
): Promise<Result<readonly Chunk[], Failure>> {
  const classified = classify(log, entries, options.now);
  if (!classified.ok) return classified;
  const acting = classified.value.filter(acts);
  if (options.newMessage && blocked(log, classified.value, options.now))
    return err({
      code: "invalid_request",
      message: "answer the interrupts first",
    });
  let raced = false;
  for (const c of acting) {
    const done = await record(ctx, principal, log, c, options.newMessage);
    if (!done.ok && !SETTLED_MEANWHILE.has(done.error.code)) return done;
    raced ||= !done.ok;
  }
  if (!raced) return ok(classified.value.flatMap(conflict));
  // Someone settled an interrupt after the read: judge every entry against the log as it is now.
  const now = await readLog(ctx, principal.tenant, log.thread);
  return resumeConflicts(now ?? log, entries, options.now);
}

/** The threads.resume_conflict frames the entries give, recording nothing. */
export function resumeConflicts(
  log: Pick<Log, "events" | "parked">,
  entries: readonly AgUiResumeEntry[],
  now: number,
): Result<readonly Chunk[], Failure> {
  const classified = classify(log, entries, now);
  return classified.ok ? ok(classified.value.flatMap(conflict)) : classified;
}

function classify(
  log: Pick<Log, "events" | "parked">,
  entries: readonly AgUiResumeEntry[],
  now: number,
): Result<readonly Classified[], Failure> {
  const out: Classified[] = [];
  for (const entry of entries) {
    const id = entry.interruptId;
    const at = interruptOf(log.events, log.parked, id, now);
    if (at.kind === "unknown")
      return err({ code: "invalid_request", message: `no interrupt ${id}` });
    if (at.kind === "other")
      return err({
        code: "forbidden",
        message: `interrupt ${id} is settled by an operator, not a resume`,
      });
    out.push({ entry, at });
  }
  return ok(out);
}

/**
 * What an entry does: an open interrupt is answered; `cancelled` for an expired challenge the
 * branch is still parked on cancels the turn, since nothing else closes that park.
 */
function acts({ entry, at }: Classified): boolean {
  if (at.state === "open") return true;
  return (
    at.kind === "approval" &&
    at.state === "expired" &&
    at.parked &&
    entry.status === "cancelled"
  );
}

/** A new message waits while an entry is open or the thread still waits on an interrupt. */
function blocked(
  log: Log,
  classified: readonly Classified[],
  now: number,
): boolean {
  if (classified.some((c) => c.at.state === "open")) return true;
  const cancelled = new Set(
    classified.filter(acts).map((c) => c.entry.interruptId),
  );
  return log.parked.some((a) => {
    if (a.kind !== "approval") return true;
    const at = interruptOf(log.events, log.parked, a.id, now);
    return at.kind === "approval" && at.state === "open"
      ? true
      : !cancelled.has(a.id);
  });
}

async function record(
  ctx: HostContext,
  principal: Principal,
  log: Log,
  { entry, at }: Classified,
  wait: boolean,
): Promise<Result<void, Failure>> {
  const id = entry.interruptId;
  if (
    at.state !== "open" ||
    (at.kind === "question" && entry.status === "cancelled")
  ) {
    const done = await log.thread.cancel(principal);
    if (!done.ok) return done;
    await resumeThread(ctx, principal, log.thread, wait);
    return ok(undefined);
  }
  if (at.kind === "approval") {
    const decision = decisionOf(entry);
    if (decision === undefined)
      return err({
        code: "invalid_request",
        message: `resume ${id}: a resolved approval's payload is an ApprovalDecision`,
      });
    const done = await decideChallenge(
      ctx,
      principal,
      log.thread,
      id,
      decision,
    );
    return done.ok ? ok(undefined) : done;
  }
  const answer = Answer.safeParse(entry.payload);
  if (!answer.success)
    return err({
      code: "invalid_request",
      message: `resume ${id}: a resolved question's payload is an Answer`,
    });
  const done = await answerQuestion(
    ctx,
    principal,
    log.thread,
    id,
    answer.data.answer,
  );
  return done.ok ? ok(undefined) : done;
}

/** A resolved entry's decision (its payload), or cancelled as a denial. */
function decisionOf(
  entry: AgUiResumeEntry,
): z.infer<typeof ApprovalDecision> | undefined {
  if (entry.status === "cancelled") return { decision: "deny" };
  const parsed = ApprovalDecision.safeParse(entry.payload);
  return parsed.success ? parsed.data : undefined;
}

/** threads.resume_conflict when a settled interrupt's entry differs from what the log holds. */
function conflict({ entry, at }: Classified): readonly Chunk[] {
  if (at.state === "open" || !differs(entry, at, at.state)) return [];
  return [
    {
      type: "CUSTOM",
      name: "threads.resume_conflict",
      value: { interruptId: entry.interruptId, recorded: at.state },
    },
  ];
}

function differs(
  entry: AgUiResumeEntry,
  at: Answerable,
  recorded: Recorded,
): boolean {
  switch (recorded) {
    case "granted":
      return decisionOf(entry)?.decision !== "grant";
    case "denied":
      return decisionOf(entry)?.decision !== "deny";
    case "expired":
    case "cancelled":
      return entry.status !== "cancelled";
    case "answered": {
      const answer = Answer.safeParse(entry.payload);
      const text = answer.success
        ? [answer.data.answer].flat().join("\n")
        : undefined;
      return at.kind !== "question" || text === undefined || text !== at.answer;
    }
    default:
      return assertNever(recorded);
  }
}
