import { HttpAgent } from "@ag-ui/client";
import type { Principal } from "@threads/core/host";
import {
  AbstractChat,
  lastAssistantMessageIsCompleteWithToolCalls as answered,
  lastAssistantMessageIsCompleteWithApprovalResponses as approved,
  type ChatInit,
  type ChatState,
  type ChatStatus,
  DefaultChatTransport,
  type UIMessage,
} from "ai";
import type { Host } from "../../src";

// The stock web clients, pointed at a host's fetch: AI SDK 7's Chat (with a plain state, as the
// React hook's without React) through DefaultChatTransport, and AG-UI's HttpAgent. No network.

/** A host's fetch: the in-process host, or a shim that forwards to one on a port. */
export type Fetcher = Pick<Host, "fetch">;

class PlainState implements ChatState<UIMessage> {
  status: ChatStatus = "ready";
  error: Error | undefined = undefined;
  messages: UIMessage[];

  constructor(messages: UIMessage[] = []) {
    this.messages = messages;
  }

  pushMessage = (message: UIMessage): void => {
    this.messages = [...this.messages, message];
  };
  popMessage = (): void => {
    this.messages = this.messages.slice(0, -1);
  };
  replaceMessage = (index: number, message: UIMessage): void => {
    this.messages = this.messages.with(index, this.snapshot(message));
  };
  snapshot = <T>(value: T): T => structuredClone(value);
}

export class TestChat extends AbstractChat<UIMessage> {
  constructor(init: ChatInit<UIMessage>) {
    super({ ...init, state: new PlainState(init.messages) });
  }
}

/** Every request as it went out, and a fetch that can drop a response after `n` bytes. */
export type Wire = {
  readonly requests: {
    readonly method: string;
    readonly url: string;
    readonly body: string;
  }[];
  /** Drops the next streamed response once it has sent this many SSE messages. */
  dropAfter: number | undefined;
};

/**
 * How a drop ends the body: a network error, or the connection closing early. The AG-UI client
 * gets the close: on an errored body, @ag-ui/client@1.0.0's teardown calls reader.cancel() and
 * rethrows its rejection, which Bun's test runner reports as an unhandled error.
 */
type Ending = "error" | "close";

export function wire(
  h: Fetcher,
  ending: Ending = "error",
): {
  readonly fetch: typeof fetch;
  readonly log: Wire;
} {
  const log: Wire = { requests: [], dropAfter: undefined };
  const call = async (
    input: string | URL | Request,
    init?: RequestInit,
  ): Promise<Response> => {
    const request = new Request(input, init);
    log.requests.push({
      method: request.method,
      url: request.url,
      body: request.method === "POST" ? await request.clone().text() : "",
    });
    const response = await h.fetch(request);
    const limit = log.dropAfter;
    if (limit === undefined || response.body === null) return response;
    log.dropAfter = undefined;
    return new Response(cut(response.body, limit, ending), response);
  };
  return { fetch: Object.assign(call, { preconnect: fetch.preconnect }), log };
}

/** The first `n` SSE messages of a body, then a network error, as a dropped connection. */
function cut(
  body: ReadableStream<Uint8Array>,
  n: number,
  ending: Ending,
): ReadableStream<Uint8Array> {
  const reader = body.getReader();
  const utf8 = new TextDecoder();
  let seen = 0;
  let text = "";
  return new ReadableStream({
    async pull(controller) {
      if (seen >= n) {
        await reader.cancel();
        if (ending === "close") controller.close();
        else
          controller.error(new TypeError("network error: connection dropped"));
        return;
      }
      const next = await reader.read();
      if (next.done) {
        controller.close();
        return;
      }
      text += utf8.decode(next.value, { stream: true });
      const blocks = text.split("\n\n");
      text = blocks.pop() ?? "";
      const keep = blocks.slice(0, n - seen);
      seen += keep.length;
      if (keep.length > 0)
        controller.enqueue(
          new TextEncoder().encode(keep.map((b) => `${b}\n\n`).join("")),
        );
    },
  });
}

export function chat(
  h: Fetcher,
  as: Principal,
  id: string,
  agent = "support",
  /** The messages the page starts with: another tab's, or a reload's restored history. */
  messages: UIMessage[] = [],
): { readonly chat: TestChat; readonly log: Wire } {
  const { fetch, log } = wire(h);
  const made = new TestChat({
    id,
    messages,
    transport: new DefaultChatTransport({
      api: `http://host.test/v1/ui/ai-sdk/${agent}`,
      headers: { "x-principal": JSON.stringify(as) },
      fetch,
    }),
    sendAutomaticallyWhen: (c) => approved(c) || answered(c),
  });
  return { chat: made, log };
}

export function agUi(
  h: Fetcher,
  as: Principal,
  threadId: string,
  agent = "support",
): { readonly agent: HttpAgent; readonly log: Wire } {
  const { fetch, log } = wire(h, "close");
  const made = new HttpAgent({
    url: `http://host.test/v1/ui/ag-ui/${agent}`,
    headers: { "x-principal": JSON.stringify(as) },
    threadId,
    fetch,
  });
  return { agent: made, log };
}

/** Until the chat has settled: nothing in flight, and `check` holds. */
export async function settled(
  c: TestChat,
  check: () => boolean = () => true,
  ms = 5_000,
): Promise<void> {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    if ((c.status === "ready" || c.status === "error") && check()) return;
    await Bun.sleep(10);
  }
  throw new Error(`chat did not settle: status ${c.status}`);
}
