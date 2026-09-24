import type { z } from "zod";
import type { Fold } from "../fold/state";
import type {
  BranchId,
  CallId,
  JsonObject,
  KnownEvent,
  PermissionRule,
} from "../log";
import type { ListedBranch } from "../store/fork-reads";

// Thread.pendingApprovals and Thread.branches: projections of the log and the branch rows.

/** host-api PendingApproval. */
export type PendingApproval = {
  readonly challenge_id: string;
  readonly call_id: CallId;
  readonly tool: string;
  readonly input: z.infer<typeof JsonObject>;
  readonly args_hash: string;
  readonly expires_at: number;
  readonly suggested_rules: readonly z.infer<typeof PermissionRule>[];
  /** Copied from the call's permission_decision: a hook's rule, or the memory_write option to change. */
  readonly reason?: string;
};

/** host-api BranchInfo. */
export type BranchInfo = {
  readonly branch_id: BranchId;
  readonly parent_branch_id?: BranchId;
  readonly fork_at_seq?: number;
  readonly mode: "live" | "stub";
  readonly runnable: boolean;
};

/** Open challenges that have not expired, oldest first. */
export function pendingApprovals(
  events: readonly KnownEvent[],
  fold: Fold,
  now: number,
): readonly PendingApproval[] {
  return events.flatMap((e): PendingApproval[] => {
    if (e.type !== "approval_requested" || e.data.expires_at <= now) return [];
    if (fold.approvals.get(e.data.challenge_id)?.consumed !== false) return [];
    const call = events.find(
      (c) => c.type === "tool_call" && c.data.call_id === e.data.call_id,
    );
    if (call?.type !== "tool_call") return [];
    // The call's latest decision: a recovery re-check records a new one.
    const decided = events.findLast(
      (d) =>
        d.type === "permission_decision" && d.data.call_id === e.data.call_id,
    );
    const reason =
      decided?.type === "permission_decision" ? decided.data.reason : undefined;
    return [
      {
        challenge_id: e.data.challenge_id,
        call_id: call.data.call_id,
        tool: call.data.name,
        input: call.data.input,
        args_hash: e.data.args_hash,
        expires_at: e.data.expires_at,
        suggested_rules: suggestedRules(call.data.name, call.data.input),
        ...(reason === undefined ? {} : { reason }),
      },
    ];
  });
}

/**
 * What an approver may keep for the thread (spec/schema/README.md, "Questions and remembered
 * rules"): a shell command exactly, then its two-word prefix form (`bash(git push:*)`); any other
 * tool as a whole. Never `bash(*)`.
 */
export function suggestedRules(
  tool: string,
  input: z.infer<typeof JsonObject>,
): readonly string[] {
  const command = input["command"];
  if (tool !== "bash" || typeof command !== "string" || command.trim() === "")
    return [tool];
  // `bash(*)` allows every command; only configured policy may say that.
  if (command === "*") return [];
  const words = shellWords(command);
  // ponytail: two-word prefix (git push, npm run); a smarter prefix needs the shell grammar.
  return words === undefined
    ? [`bash(${command})`]
    : [`bash(${command})`, `bash(${words.slice(0, 2).join(" ")}:*)`];
}

// A shell word: bare characters, a backslash escape, a single-quoted or a double-quoted run.
const WORD = /(?:[^\s\\'"]+|\\[\s\S]|'[^']*'|"(?:[^"\\]|\\[\s\S])*")+/g;
const PART = /\\([\s\S])|'([^']*)'|"((?:[^"\\]|\\[\s\S])*)"/g;

/**
 * POSIX shell words as Python's shlex.split makes them; undefined when a quote is unclosed or a
 * backslash ends the text.
 */
function shellWords(text: string): readonly string[] | undefined {
  if (text.replace(WORD, "").trim() !== "") return undefined;
  return (text.match(WORD) ?? []).map((word) =>
    word.replace(
      PART,
      (_all, escaped?: string, single?: string, double?: string) =>
        // Inside double quotes a backslash escapes only `"` and itself.
        escaped ?? single ?? double?.replace(/\\(["\\])/g, "$1") ?? "",
    ),
  );
}

// ponytail: every branch reports mode live; a stub fork records no mode yet.
export function branchInfo(row: ListedBranch): BranchInfo {
  return {
    branch_id: row.branch_id,
    ...(row.parent_branch_id === null
      ? {}
      : { parent_branch_id: row.parent_branch_id }),
    ...(row.fork_at_seq === null ? {} : { fork_at_seq: row.fork_at_seq }),
    mode: "live",
    runnable: row.state === "ready",
  };
}
