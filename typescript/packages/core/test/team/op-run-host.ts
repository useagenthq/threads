import { z } from "zod";
import { ThreadId } from "../../src/log";
import type { Writer } from "../../src/store/writer";
import { teamOfLog } from "../../src/team/rows";
import { supervise } from "../../src/team/supervise";
import { turnFailure } from "../../src/team/turn-failure";
import { Failure, Hop, Policy, TurnEnd } from "./op-inputs";
import { decided, type Op } from "./op-run";

// The Teams Phase 2 ops of the vector suite (spec/conformance/vectors/team-ops.json): a host
// member's turn-only failure, and the supervisor's one decision on an ended generation. Their
// own module so op-run.ts stays the runner and these stay the two host ops.

/** A host member's turn-only failure (rule 53): the ending append under its own writer. */
export async function turnFailureOp(w: Writer, v: Op): Promise<unknown> {
  const hop = Hop.optional().parse(v.input["hop"]);
  let out: unknown;
  await decided(w, async (ctx) => {
    out = await turnFailure(
      ctx,
      TurnEnd.parse(v.input["turn"]),
      Failure.parse(v.input["error"]),
      ...(hop === undefined
        ? []
        : ([
            { ...hop, scope: "hop", observed_is_upper_bound: false },
          ] as const)),
    );
  });
  return out;
}

/**
 * The supervisor step (rule 51): the host team log's one decision on an ended generation, with
 * the restart's new thread minted before the append as a start's is.
 */
export async function superviseOp(w: Writer, v: Op): Promise<unknown> {
  const policy = Policy.parse(v.input["policy"]);
  const member = z.string().parse(v.input["member"]);
  const given = v.input["thread_id"];
  let out: unknown;
  await decided(w, async (ctx) => {
    const team = await teamOfLog(ctx.tx, ctx.branchId);
    if (team === undefined) throw new Error(`no team log ${ctx.branchId}`);
    // A stop starts nothing, so its vector names no thread: the team log's own stands in.
    const threadId = ThreadId.parse(given ?? ctx.threadId);
    out = await supervise(
      { tx: ctx.tx, fold: ctx.chain.fold, batch: ctx.batch, team, threadId },
      member,
      policy,
    );
  });
  return out;
}
