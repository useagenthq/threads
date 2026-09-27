import type { Sent, WebTransport } from "@threads/core/adapter";
import { A2A_VERSION, IDEMPOTENT_SEND } from "../../src/protocol";

// A partner that records every request it was asked to send, and can lose an answer on purpose.
// The counts are the point: a drill that cannot say how many times we sent proves nothing about
// invariant 3, so every request is appended here and asserted in the test.

export const CARD_URL = "https://partner.example/.well-known/agent-card.json";
export const RPC_URL = "https://partner.example/a2a/refunds";

export type Request = {
  readonly method: string;
  readonly url: string;
  readonly body: string | undefined;
  readonly headers: Readonly<Record<string, string>>;
};

export type Partner = WebTransport & {
  /** Every request, in order, by operation: `sends` is the one that must never repeat. */
  readonly requests: Request[];
  readonly sends: () => readonly Request[];
  /** The tasks the partner holds, by id, as ListTasks and GetTask would answer. */
  readonly tasks: Map<string, Task>;
  /** What the next `SendMessage` does. Set per drill. */
  send: (body: string) => Answer;
  /** Runs once the partner's answer is on the wire: where a drill kills the process. */
  onAnswered: (() => Promise<void>) | undefined;
  /**
   * While true, `ListTasks` answers an empty page even though the task exists: the peer is still
   * creating it, or has truncated its history. This is the case that must never settle a park.
   */
  hidden: boolean;
};

export type Task = {
  id: string;
  contextId: string;
  status: { state: string; message?: unknown };
  /** Full A2A messages: a partner's history is what reconciliation looks our messageId up in. */
  history?: readonly unknown[];
  artifacts?: readonly unknown[];
};

/** What one scripted `SendMessage` does: answer, lose the answer, or refuse the connection. */
export type Answer =
  | { readonly kind: "task"; readonly task: Task }
  | { readonly kind: "lost"; readonly task: Task; readonly code: string }
  | { readonly kind: "refused"; readonly code: string };

export function partner(card: unknown = defaultCard()): Partner {
  const requests: Request[] = [];
  const tasks = new Map<string, Task>();
  const it: Partner = {
    requests,
    tasks,
    hidden: false,
    onAnswered: undefined,
    sends: () => requests.filter((r) => r.method === "SendMessage"),
    send: () => {
      throw new Error("the drill sets partner.send");
    },
    resolve: async () => ["93.184.216.34"],
    fetch: async (url, _address, init) => {
      const method = operationOf(url, init);
      requests.push({
        method,
        url,
        body: init.body,
        headers: init.headers,
      });
      if (method === "card") return Response.json(card);
      if (method === "SendMessage") {
        const answer = sending(it, init.body ?? "");
        await it.onAnswered?.();
        return answer;
      }
      if (method === "ListTasks") {
        const listed = it.hidden ? [] : [...tasks.values()];
        return Response.json({
          tasks: listed,
          nextPageToken: "",
          pageSize: 50,
          totalSize: listed.length,
        });
      }
      if (method === "GetTask") return Response.json({ task: found(it, url) });
      return Response.json({ code: -32601, message: method }, { status: 404 });
    },
  };
  return it;
}

function sending(it: Partner, body: string): Response {
  const answer = it.send(body);
  if (answer.kind === "refused") throw coded(answer.code);
  // The task exists at the partner either way: what a "lost" answer loses is our knowledge of it.
  it.tasks.set(answer.task.id, {
    ...answer.task,
    history: [
      ...(answer.task.history ?? []),
      {
        messageId: messageIdIn(body),
        role: "ROLE_USER",
        parts: [{ text: "what we asked" }],
      },
    ],
  });
  if (answer.kind === "lost") throw coded(answer.code);
  return Response.json({ task: it.tasks.get(answer.task.id) });
}

function found(it: Partner, url: string): Task | undefined {
  const id = decodeURIComponent(new URL(url).pathname.split("/").at(-1) ?? "");
  return it.tasks.get(id) ?? [...it.tasks.values()][0];
}

function coded(code: string): Error {
  return Object.assign(new Error(code), { code });
}

/** The messageId the body carries: what a peer would deduplicate on. */
export function messageIdIn(body: string): string {
  const parsed: unknown = JSON.parse(body);
  const params = at(at(parsed, "params") ?? parsed, "message");
  const id = at(params, "messageId");
  return typeof id === "string" ? id : "";
}

function at(value: unknown, key: string): unknown {
  return typeof value === "object" && value !== null && key in value
    ? Reflect.get(value, key)
    : undefined;
}

/** Which A2A operation a request is, from the URL and the JSON-RPC method in the body. */
function operationOf(url: string, init: Sent): string {
  if (url === CARD_URL) return "card";
  if (init.method === "GET")
    return new URL(url).pathname.endsWith("/tasks") ? "ListTasks" : "GetTask";
  const method = at(JSON.parse(init.body ?? "{}"), "method");
  return typeof method === "string" ? method : "unknown";
}

export function defaultCard(extension?: {
  readonly window_ms: number;
}): unknown {
  return {
    name: "refunds",
    description: "The partner's refunds desk.",
    version: "1.0.0",
    capabilities: {
      streaming: false,
      ...(extension === undefined
        ? {}
        : {
            extensions: [{ uri: IDEMPOTENT_SEND, params: extension }],
          }),
    },
    defaultInputModes: ["text/plain"],
    defaultOutputModes: ["text/plain"],
    skills: [],
    supportedInterfaces: [
      {
        url: RPC_URL,
        protocolBinding: "JSONRPC",
        protocolVersion: A2A_VERSION,
      },
    ],
  };
}

export function task(id: string, state: string, text?: string): Task {
  return {
    id,
    contextId: "ignored-by-us",
    status: {
      state,
      ...(text === undefined
        ? {}
        : {
            message: {
              messageId: `m-${id}`,
              role: "ROLE_AGENT",
              parts: [{ text }],
            },
          }),
    },
  };
}
