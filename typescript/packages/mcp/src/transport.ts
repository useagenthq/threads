import type {
  Transport,
  TransportSendOptions,
} from "@modelcontextprotocol/sdk/shared/transport.js";
import type { JSONRPCMessage } from "@modelcontextprotocol/sdk/types.js";
import { fenceHere } from "@threads/core/adapter";

// The fence at the real transport, as for model and sandbox adapters. Over
// HTTP every request the SDK makes goes through a wrapped fetch; a stdio server runs on the
// host, where it can't be fenced remotely, so each message write to it is fenced instead. Both
// re-check the lease as the bytes would leave, inside the operation `within` opened. Outside
// one, nothing leaves (a stray SDK request is refused, never sent).

/** What the SDK's client transports have in common, as the client uses it. */
type Inner = {
  start(): Promise<void>;
  close(): Promise<void>;
  send(message: JSONRPCMessage, options?: TransportSendOptions): Promise<void>;
  onmessage?: Transport["onmessage"];
  onclose?: Transport["onclose"];
  onerror?: Transport["onerror"];
  readonly sessionId?: string | undefined;
  setProtocolVersion?: (version: string) => void;
};

/**
 * The client's view of a transport. `fenceWrites`: each message write passes the fence first
 * (stdio). Without it the writes go straight through (HTTP, fenced at its fetch instead).
 */
export function clientTransport(inner: Inner, fenceWrites: boolean): Transport {
  const outer: Transport = {
    start: () => inner.start(),
    close: () => inner.close(),
    send: async (message: JSONRPCMessage, options) => {
      if (fenceWrites) await fenceHere();
      return inner.send(message, options);
    },
    setProtocolVersion: (version) => inner.setProtocolVersion?.(version),
  };
  Object.defineProperty(outer, "sessionId", { get: () => inner.sessionId });
  inner.onmessage = (message, extra) => outer.onmessage?.(message, extra);
  inner.onclose = () => outer.onclose?.();
  inner.onerror = (error) => outer.onerror?.(error);
  return outer;
}
