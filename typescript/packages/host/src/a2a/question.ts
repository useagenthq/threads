import { type KnownEvent, questionText } from "@threads/core/host";
import { z } from "zod";

// The ask_user question a run has open, derived from the events alone. A frame must show the
// question that was open where it was read, not the one open now, so this is read from a prefix of
// the log rather than from the fold's current view. It only renders a question; the rules for
// matching an answer to one stay in core, where Thread.answer applies them.

/** ask_user's input as core renders it, so the wording a caller sees is core's, not ours. */
export type Ask = Parameters<typeof questionText>[0];

const Input = z.looseObject({
  question: z.string(),
  options: z.array(z.string()).optional(),
  multi_select: z.boolean().optional(),
});

export type OpenAsk = { readonly callId: string; readonly ask: Ask };

/**
 * The question open at the end of `events`: the oldest input park no resumed has closed, with the
 * ask its tool_call recorded. Undefined when nothing is waiting on an answer.
 */
export function openAsk(events: readonly KnownEvent[]): OpenAsk | undefined {
  const open: string[] = [];
  for (const e of events) {
    if (e.type === "parked" && e.data.address.kind === "input")
      open.push(e.data.address.id);
    else if (e.type === "resumed" && e.data.address.kind === "input") {
      const at = open.indexOf(e.data.address.id);
      if (at !== -1) open.splice(at, 1);
    }
  }
  const callId = open[0];
  if (callId === undefined) return undefined;
  const call = events.find(
    (e) => e.type === "tool_call" && e.data.call_id === callId,
  );
  if (call?.type !== "tool_call") return undefined;
  const parsed = Input.safeParse(call.data.input);
  if (!parsed.success) return undefined;
  const { question, options, multi_select } = parsed.data;
  return {
    callId,
    ask: {
      question,
      ...(options === undefined ? {} : { options }),
      ...(multi_select === undefined ? {} : { multi_select }),
    },
  };
}

/** The question and its choices as core words them: an INPUT_REQUIRED task's status message. */
export function askText(ask: Ask): string {
  return questionText(ask);
}
