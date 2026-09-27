import { describe, expect, test } from "bun:test";
import { pinCard } from "../src/protocol/pin";

// Pinning chooses, once, what we will speak to a partner for the rest of the conversation. The
// failure to guard against is pinning something we can never call: the send-time SSRF guard would
// catch it, but by then every call on that remote answers `not_sent` forever, so an unusable URL has
// to lose the selection here rather than win it and fail later.

const CARD_URL = "https://partner.example/.well-known/agent-card.json";

type Interface = {
  readonly url: string;
  readonly protocolBinding: string;
  readonly protocolVersion: string;
};

const rpc = (url: string): Interface => ({
  url,
  protocolBinding: "JSONRPC",
  protocolVersion: "1.0",
});

const rest = (url: string): Interface => ({
  url,
  protocolBinding: "HTTP+JSON",
  protocolVersion: "1.0",
});

function card(interfaces: readonly Interface[]): Uint8Array {
  return new TextEncoder().encode(
    JSON.stringify({
      name: "partner",
      description: "a partner",
      version: "1",
      supportedInterfaces: interfaces,
      capabilities: {},
      defaultInputModes: ["text/plain"],
      defaultOutputModes: ["text/plain"],
      skills: [],
    }),
  );
}

/** The pinned wire, or a thrown error naming why the card was refused. */
function wireOf(interfaces: readonly Interface[]): { readonly url: string } {
  const pinned = pinCard(card(interfaces), CARD_URL, false);
  if (!("wire" in pinned)) throw new Error(`${pinned.code}: ${pinned.message}`);
  return pinned.wire;
}

describe("the interface a card pins", () => {
  test("a usable https interface is pinned", () => {
    expect(wireOf([rpc("https://partner.example/a2a")]).url).toBe(
      "https://partner.example/a2a",
    );
  });

  test("a URL with credentials loses to the safe one behind it", () => {
    // The card offers the unusable URL FIRST, which is the whole point: taking the first match
    // pinned this and every later call answered not_sent.
    expect(
      wireOf([
        rpc("https://user:pass@10.0.0.1/a2a"),
        rpc("https://partner.example/a2a"),
      ]).url,
    ).toBe("https://partner.example/a2a");
  });

  test("a private IP literal loses to the safe one behind it", () => {
    expect(
      wireOf([rpc("https://10.0.0.1/a2a"), rpc("https://partner.example/a2a")])
        .url,
    ).toBe("https://partner.example/a2a");
  });

  test("a loopback literal loses, in either family", () => {
    expect(
      wireOf([
        rpc("https://127.0.0.1/a2a"),
        rpc("https://[::1]/a2a"),
        rpc("https://partner.example/a2a"),
      ]).url,
    ).toBe("https://partner.example/a2a");
  });

  test("an http interface loses: a remote is called over https", () => {
    expect(
      wireOf([
        rpc("http://partner.example/a2a"),
        rpc("https://partner.example/a2a"),
      ]).url,
    ).toBe("https://partner.example/a2a");
  });

  test("a binding we prefer less is taken when the preferred one is unusable", () => {
    // JSON-RPC is preferred, but this card's only JSON-RPC interface is one we can never call.
    const pinned = pinCard(
      card([rpc("https://10.0.0.1/a2a"), rest("https://partner.example/a2a")]),
      CARD_URL,
      false,
    );
    if (!("wire" in pinned)) throw new Error(pinned.message);
    expect(pinned.wire).toEqual({
      url: "https://partner.example/a2a",
      binding: "HTTP+JSON",
    });
  });

  test("a card whose every interface is unusable is remote_unsupported, and says why of each", () => {
    const pinned = pinCard(
      card([
        rpc("https://user:pass@10.0.0.1/a2a"),
        rest("http://partner.example/a2a"),
      ]),
      CARD_URL,
      false,
    );
    expect("wire" in pinned).toBe(false);
    if ("wire" in pinned) throw new Error("unreachable");
    expect(pinned.code).toBe("remote_unsupported");
    expect(pinned.message).toContain("credentials");
    expect(pinned.message).toContain("https");
  });

  test("a DNS name is not judged here: it is resolved by the send-time guard", () => {
    // Deciding a name now would pin a card against one moment's DNS, and a card is pinned once and
    // used for a long time. `vet` checks every address the name resolves to, at send time.
    expect(wireOf([rpc("https://internal-only.partner.example/a2a")]).url).toBe(
      "https://internal-only.partner.example/a2a",
    );
  });
});
