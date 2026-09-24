import type { EventOf, KnownEvent } from "@threads/core/host";

// The committed events a UI stream shows (spec/schema/ui/README.md, "Mapping"). Every other
// event maps to zero frames.

export type Framed = EventOf<
  | "model_request"
  | "model_response"
  | "model_response_recovered"
  | "model_attempt_abandoned"
  | "approval_requested"
  | "approval_granted"
  | "approval_denied"
  | "tool_result"
  | "tool_result_late"
  | "agent_spawned"
  | "agent_finished"
  | "retry_scheduled"
>;

const FRAMED = {
  model_request: true,
  model_response: true,
  model_response_recovered: true,
  model_attempt_abandoned: true,
  approval_requested: true,
  approval_granted: true,
  approval_denied: true,
  tool_result: true,
  tool_result_late: true,
  agent_spawned: true,
  agent_finished: true,
  retry_scheduled: true,
} satisfies Record<Framed["type"], true>;

export function isFramed(e: KnownEvent): e is Framed {
  return Object.hasOwn(FRAMED, e.type);
}

/** A model step's events: its request, then its response or abandonment. */
export type ModelEvent = EventOf<
  | "model_request"
  | "model_response"
  | "model_response_recovered"
  | "model_attempt_abandoned"
>;

type Response = EventOf<"model_response" | "model_response_recovered">;

/** A text or reasoning part's id: the model_request's event id and the part's index. */
export function partId(requestId: string, index: number): string {
  return `${requestId}:${index}`;
}

/** Each response part that shows, with its index in the response's content. */
export function shownParts(e: Response): readonly Shown[] {
  return e.data.content.flatMap((part, index): Shown[] => {
    if (part.type === "text")
      return part.text === "" ? [] : [{ kind: "text", index, text: part.text }];
    if (part.type === "reasoning")
      return part.summary === undefined || part.summary === ""
        ? []
        : [{ kind: "reasoning", index, text: part.summary }];
    if (part.type === "tool_use")
      return [
        {
          kind: "tool",
          index,
          callId: part.call_id,
          name: part.name,
          input: part.input,
        },
      ];
    return [];
  });
}

export type Shown =
  | {
      readonly kind: "text" | "reasoning";
      readonly index: number;
      readonly text: string;
    }
  | {
      readonly kind: "tool";
      readonly index: number;
      readonly callId: string;
      readonly name: string;
      readonly input: EventOf<"tool_call">["data"]["input"];
    };
