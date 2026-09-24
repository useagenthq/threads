import type { Json } from "@threads/core/host";
import { z } from "zod";
import type { Chunk } from "./frame";

// foldAgUi: the messages @ag-ui/client@1.0.0's defaultApplyEvents builds from a stream, ported
// for the events threads sends (spec/schema/ui/README.md, "Snapshot"). A replay's
// MESSAGES_SNAPSHOT is this fold of the canonical frames, so it equals what an uninterrupted
// stock client holds by construction. Pinned to that version: a test runs every ui case through
// both this and the real client, and a weekly job re-checks against @ag-ui/client@latest.

type ToolCall = {
  readonly id: string;
  readonly type: "function";
  readonly function: { readonly name: string; arguments: string };
};

/** A message as the stock client holds it; `content` grows as deltas append. */
export type AgUiMessage = {
  readonly id: string;
  readonly role: string;
  content?: string;
  toolCalls?: ToolCall[];
  readonly toolCallId?: string;
};

export type UserTurn = {
  readonly id: string;
  readonly text: string;
  readonly frames: readonly Chunk[];
};

/** The stock client's messages after each turn's user message and its frames, in order. */
export function foldAgUi(turns: readonly UserTurn[]): readonly AgUiMessage[] {
  const fold = new AgUiFold();
  for (const turn of turns) {
    fold.messages.push({ id: turn.id, role: "user", content: turn.text });
    for (const chunk of turn.frames) fold.apply(chunk);
  }
  return fold.messages;
}

export class AgUiFold {
  messages: AgUiMessage[];

  constructor(messages: readonly AgUiMessage[] = []) {
    this.messages = messages.map((m) => structuredClone(m));
  }

  apply(e: Chunk): void {
    switch (e.type) {
      case "TEXT_MESSAGE_START":
        this.#open(text(e, "messageId"), text(e, "role") || "assistant");
        return;
      case "REASONING_MESSAGE_START":
        this.#open(text(e, "messageId"), "reasoning");
        return;
      case "TEXT_MESSAGE_CONTENT":
      case "REASONING_MESSAGE_CONTENT":
        this.#append(text(e, "messageId"), text(e, "delta"));
        return;
      case "TOOL_CALL_START":
        this.#call(e);
        return;
      case "TOOL_CALL_ARGS":
        this.#args(text(e, "toolCallId"), text(e, "delta"));
        return;
      case "TOOL_CALL_RESULT":
        this.#result(e);
        return;
      case "MESSAGES_SNAPSHOT":
        this.#merge(e["messages"]);
        return;
      default:
        return;
    }
  }

  #open(id: string, role: string): void {
    if (!this.messages.some((m) => m.id === id))
      this.messages.push({ id, role, content: "" });
  }

  #append(id: string, delta: string): void {
    const target = this.messages.find((m) => m.id === id);
    if (target !== undefined)
      target.content = `${target.content ?? ""}${delta}`;
  }

  #owner(callId: string): AgUiMessage | undefined {
    return this.messages.find((m) => m.toolCalls?.some((c) => c.id === callId));
  }

  #call(e: Chunk): void {
    const callId = text(e, "toolCallId");
    if (this.#owner(callId) !== undefined) return;
    const parent = e["parentMessageId"];
    const existing =
      typeof parent === "string"
        ? this.messages.find((m) => m.id === parent)
        : undefined;
    let owner = existing?.role === "assistant" ? existing : undefined;
    if (owner === undefined) {
      const id =
        typeof parent === "string" && existing === undefined ? parent : callId;
      owner = { id, role: "assistant", toolCalls: [] };
      this.messages.push(owner);
    }
    owner.toolCalls ??= [];
    owner.toolCalls.push({
      id: callId,
      type: "function",
      function: { name: text(e, "toolCallName"), arguments: "" },
    });
  }

  #args(callId: string, delta: string): void {
    const call = this.#owner(callId)?.toolCalls?.find((c) => c.id === callId);
    if (call !== undefined) call.function.arguments += delta;
  }

  /** A result goes right after its call's message and any results already there. */
  #result(e: Chunk): void {
    const toolCallId = text(e, "toolCallId");
    const message: AgUiMessage = {
      id: text(e, "messageId"),
      role: "tool",
      toolCallId,
      content: text(e, "content"),
    };
    const owner = this.messages.findIndex(
      (m) =>
        m.role === "assistant" && m.toolCalls?.some((c) => c.id === toolCallId),
    );
    if (owner === -1) {
      this.messages.push(message);
      return;
    }
    let at = owner + 1;
    while (this.messages[at]?.role === "tool") at += 1;
    this.messages.splice(at, 0, message);
  }

  /** A merge by id: kept in place when named, dropped when not, appended when new. */
  #merge(raw: Json | undefined): void {
    const incoming = snapshotMessages(raw);
    const byId = new Map(incoming.map((m) => [m.id, m]));
    const keepsReasoning = !incoming.some((m) => m.role === "reasoning");
    const kept = this.messages
      .filter(
        (m) => byId.has(m.id) || (keepsReasoning && m.role === "reasoning"),
      )
      .map((m) => byId.get(m.id) ?? m);
    const ids = new Set(kept.map((m) => m.id));
    this.messages = [...kept, ...incoming.filter((m) => !ids.has(m.id))];
  }
}

function text(e: Chunk, key: string): string {
  const value = e[key];
  return typeof value === "string" ? value : "";
}

/** A snapshot's messages, as the fold holds them; anything else in them is left out. */
const Snapshot = z.array(
  z.looseObject({
    id: z.string(),
    role: z.string(),
    content: z.string().optional(),
    toolCallId: z.string().optional(),
    toolCalls: z
      .array(
        z.looseObject({
          id: z.string(),
          function: z.looseObject({ name: z.string(), arguments: z.string() }),
        }),
      )
      .optional(),
  }),
);

function snapshotMessages(raw: Json | undefined): AgUiMessage[] {
  const parsed = Snapshot.safeParse(raw);
  if (!parsed.success) return [];
  return parsed.data.map((m) => ({
    id: m.id,
    role: m.role,
    ...(m.content === undefined ? {} : { content: m.content }),
    ...(m.toolCallId === undefined ? {} : { toolCallId: m.toolCallId }),
    ...(m.toolCalls === undefined
      ? {}
      : {
          toolCalls: m.toolCalls.map((c) => ({
            id: c.id,
            type: "function" as const,
            function: {
              name: c.function.name,
              arguments: c.function.arguments,
            },
          })),
        }),
  }));
}
