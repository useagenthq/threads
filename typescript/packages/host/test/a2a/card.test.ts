import { afterEach, describe, expect, test } from "bun:test";
import { A2A_JSON, AgentCard } from "@threads/a2a/protocol";
import { reader, talker } from "./agents";
import { serve, stopAll } from "./kit";

// The card is what we publish about an agent, so the failure to guard against is saying too much.
// It carries no instructions, no tool names and no model name, its interface URLs come from the
// origin it was asked for, and it is readable without any credential at all.

afterEach(stopAll);

const ONE = { support: { description: "Answers questions about orders." } };

test("the card is byte-exact for a fixture host", async () => {
  const on = await serve({
    agents: { support: talker("hi") },
    a2a: { expose: ONE },
  });
  const response = await on.raw(
    "GET",
    "/a2a/support/.well-known/agent-card.json",
  );
  expect(response.status).toBe(200);
  expect(response.headers.get("content-type")).toBe(A2A_JSON);
  const text = await response.text();
  // The version is the agent's pinned config hash, so it moves with the agent rather than with
  // this file; every other byte, field order included, is pinned here. The two tests below pin
  // what the version means, which is the part a golden hash would only pin by accident.
  const version = AgentCard.parse(JSON.parse(text)).version;
  expect(version).toMatch(/^[0-9a-f]{64}$/);
  // Canonical JSON, so this is the card's exact bytes and not a shape that happens to match.
  expect(text).toBe(
    '{"capabilities":{"extendedAgentCard":false,"extensions":[{"description":"A SendMessage that repeats a messageId from the same authenticated caller returns the original task and starts nothing.","params":{"window_ms":604800000},"uri":"https://threadsai.dev/a2a/ext/idempotent-send/v1"}],"pushNotifications":false,"streaming":true},"defaultInputModes":["text/plain","application/json"],"defaultOutputModes":["text/plain","application/json"],"description":"Answers questions about orders.","name":"support","securityRequirements":[{"schemes":{"bearer":{"list":[]}}}],"securitySchemes":{"bearer":{"httpAuthSecurityScheme":{"scheme":"bearer"}}},"skills":[{"description":"Answers questions about orders.","id":"support","name":"support","tags":["text"]}],"supportedInterfaces":[{"protocolBinding":"JSONRPC","protocolVersion":"1.0","url":"http://host.test/a2a/support"},{"protocolBinding":"HTTP+JSON","protocolVersion":"1.0","url":"http://host.test/a2a/support"}],"version":' +
      JSON.stringify(version) +
      "}",
  );
});

describe("what the card says about authentication", () => {
  test("a declared scheme comes with the requirement that says it is required", async () => {
    // Declaring a scheme and no requirement is what a conforming partner reads as "nothing is
    // required": it calls unauthenticated and gets our 401. Ours infers "auth required" from the
    // scheme set being non-empty, which is exactly why this was invisible from inside.
    const on = await serve({
      agents: { support: talker("hi") },
      a2a: { expose: ONE },
    });
    const card: Record<string, unknown> = await (
      await on.raw("GET", "/a2a/support/.well-known/agent-card.json")
    ).json();
    expect(card["securitySchemes"]).toEqual({
      bearer: { httpAuthSecurityScheme: { scheme: "bearer" } },
    });
    expect(card["securityRequirements"]).toEqual([
      { schemes: { bearer: { list: [] } } },
    ]);
  });

  test("the requirement is derived from the declared names, not hardcoded to bearer", async () => {
    const on = await serve({
      agents: { support: talker("hi") },
      a2a: {
        expose: ONE,
        securitySchemes: {
          oidc: {
            openIdConnectSecurityScheme: {
              openIdConnectUrl: "https://idp.example",
            },
          },
          key: { apiKeySecurityScheme: { location: "header", name: "X-Key" } },
        },
      },
    });
    const card: Record<string, unknown> = await (
      await on.raw("GET", "/a2a/support/.well-known/agent-card.json")
    ).json();
    // One requirement per declared scheme, so satisfying any one of them is enough.
    expect(card["securityRequirements"]).toEqual([
      { schemes: { key: { list: [] } } },
      { schemes: { oidc: { list: [] } } },
    ]);
  });
});

async function versionOf(
  agents: Parameters<typeof serve>[0]["agents"],
): Promise<string> {
  const on = await serve({ agents, a2a: { expose: ONE } });
  const card = AgentCard.parse(
    await (
      await on.raw("GET", "/a2a/support/.well-known/agent-card.json")
    ).json(),
  );
  return card.version;
}

test("the card's version is the same for the same agent config", async () => {
  expect(await versionOf({ support: talker("hi") })).toBe(
    await versionOf({ support: talker("hi") }),
  );
});

test("the card's version changes when the agent's config changes", async () => {
  // A different tool set is a different agent, so a client that caches by version refetches.
  expect(await versionOf({ support: talker("hi") })).not.toBe(
    await versionOf({ support: reader("hi") }),
  );
});

test("the card publishes no instructions, no tool name and no model name", async () => {
  // Every part of the agent that must never be published, in one agent.
  const on = await serve({
    agents: { support: talker("hi") },
    a2a: { expose: ONE },
  });
  const text = await (
    await on.raw("GET", "/a2a/support/.well-known/agent-card.json")
  ).text();
  for (const secret of [
    "Never publish me.",
    "lookup_order",
    "ask_user",
    "final_output",
    "scripted",
    "instructions",
    "tools",
    "model",
  ])
    expect(text).not.toInclude(secret);
});

test("the card parses as an A2A 1.0 agent card", async () => {
  const on = await serve({
    agents: { support: talker("hi") },
    a2a: { expose: ONE },
  });
  const body: unknown = await (
    await on.raw("GET", "/a2a/support/.well-known/agent-card.json")
  ).json();
  const card = AgentCard.parse(body);
  expect(card.capabilities.pushNotifications).toBe(false);
  expect(card.capabilities.extendedAgentCard).toBe(false);
  expect(card.signatures).toBeUndefined();
  // No AgentInterface.tenant, which is why the {tenant} path variants are not served.
  expect(card.supportedInterfaces.every((i) => i.tenant === undefined)).toBe(
    true,
  );
});

test("the interface URLs come from the origin the card was asked for", async () => {
  const on = await serve({
    agents: { support: talker("hi") },
    a2a: { expose: ONE },
  });
  const response = await on.host.fetch(
    new Request(
      "https://partner.example:8443/a2a/support/.well-known/agent-card.json",
    ),
  );
  const card = AgentCard.parse(await response.json());
  expect(card.supportedInterfaces.map((i) => i.url)).toEqual([
    "https://partner.example:8443/a2a/support",
    "https://partner.example:8443/a2a/support",
  ]);
});

test("the card is readable with no authenticate configured and no credential", async () => {
  const on = await serve({
    agents: { support: talker("hi") },
    a2a: { expose: ONE },
    withAuth: false,
  });
  const response = await on.raw(
    "GET",
    "/a2a/support/.well-known/agent-card.json",
  );
  expect(response.status).toBe(200);
  expect(AgentCard.parse(await response.json()).name).toBe("support");
});

test("the single-agent alias serves the one exposed agent's card", async () => {
  const on = await serve({
    agents: { support: talker("hi") },
    a2a: { expose: ONE },
  });
  const alias = await on.raw("GET", "/.well-known/agent-card.json");
  expect(alias.status).toBe(200);
  const card = AgentCard.parse(await alias.json());
  expect(card.name).toBe("support");
  // Found at the root, but the endpoints it names are still the agent's own absolute URLs.
  expect(card.supportedInterfaces.map((i) => i.url)).toEqual([
    "http://host.test/a2a/support",
    "http://host.test/a2a/support",
  ]);
});

test("the alias is not served when two agents are exposed", async () => {
  const on = await serve({
    agents: { support: talker("hi"), helper: talker("hi") },
    a2a: {
      expose: {
        support: { description: "Support." },
        helper: { description: "Help." },
      },
    },
  });
  const alias = await on.raw("GET", "/.well-known/agent-card.json");
  expect(alias.status).toBe(404);
  // Each agent's own card is still there, so nothing was lost by dropping the alias.
  for (const name of ["support", "helper"])
    expect(
      (await on.raw("GET", `/a2a/${name}/.well-known/agent-card.json`)).status,
    ).toBe(200);
});

test("a card for an agent that is not exposed is 404", async () => {
  const on = await serve({
    agents: { support: talker("hi"), helper: talker("hi") },
    a2a: { expose: ONE },
  });
  expect(
    (await on.raw("GET", "/a2a/helper/.well-known/agent-card.json")).status,
  ).toBe(404);
});

test("the securitySchemes option replaces the default bearer declaration", async () => {
  const on = await serve({
    agents: { support: talker("hi") },
    a2a: {
      expose: ONE,
      securitySchemes: {
        corp: { oauth2SecurityScheme: { flows: { clientCredentials: {} } } },
      },
    },
  });
  const card = AgentCard.parse(
    await (
      await on.raw("GET", "/a2a/support/.well-known/agent-card.json")
    ).json(),
  );
  expect(Object.keys(card.securitySchemes ?? {})).toEqual(["corp"]);
});
