import { afterEach, expect, test } from "bun:test";
import { alice, eve } from "../kit";
import { talker } from "./agents";
import { faultName, message, serve, stopAll, task } from "./kit";

// The checks every route but the card makes, in the order the design fixes them: version, then
// authenticate, then whether the agent is exposed, then the tenant. Each is asserted by the answer
// a partner would get, in both bindings, not by reaching into the route.

afterEach(stopAll);

const ONE = { support: { description: "Support." } };

const up = () =>
  serve({ agents: { support: talker("hi", "hi") }, a2a: { expose: ONE } });

test("no A2A-Version in header or query is VersionNotSupportedError, never a default to 1.0", async () => {
  const on = await up();
  const rpc = await on.rpc(
    "GetTask",
    { id: "x" },
    { as: alice, version: null },
  );
  expect(await faultName(rpc)).toBe("VersionNotSupportedError");
  const http = await on.http("GET", "/tasks/x", { as: alice, version: null });
  expect(http.status).toBe(400);
  expect(await faultName(http)).toBe("VersionNotSupportedError");
});

test("a wrong A2A-Version is refused in the header and in the query", async () => {
  const on = await up();
  const header = await on.rpc(
    "GetTask",
    { id: "x" },
    { as: alice, version: "0.3" },
  );
  expect(await faultName(header)).toBe("VersionNotSupportedError");
  const query = await on.http("GET", "/tasks/x", {
    as: alice,
    version: null,
    versionQuery: "0.3",
  });
  expect(await faultName(query)).toBe("VersionNotSupportedError");
});

test("the version is compared as Major.Minor, so 1.0.1 is 1.0", async () => {
  const on = await up();
  const sent = await on.rpc("SendMessage", message("m1", "hello"), {
    as: alice,
    version: "1.0.1",
  });
  expect((await task(sent)).id).toBeString();
});

test("the version may arrive as the query parameter instead of the header", async () => {
  const on = await up();
  const sent = await on.http("POST", "/message:send", {
    as: alice,
    version: null,
    versionQuery: "1.0",
    body: message("m-q", "hello"),
  });
  expect((await task(sent)).id).toBeString();
});

test("an unauthenticated request is 401 with a Bearer challenge in both bindings", async () => {
  const on = await up();
  const rpc = await on.rpc("GetTask", { id: "x" }, {});
  expect(rpc.status).toBe(401);
  expect(rpc.headers.get("www-authenticate")).toBe("Bearer");
  expect(await faultName(rpc)).toBe("InvalidRequestError");
  const http = await on.http("GET", "/tasks/x", {});
  expect(http.status).toBe(401);
  expect(http.headers.get("www-authenticate")).toBe("Bearer");
  expect(await faultName(http)).toBe("InvalidRequestError");
});

test("an unauthenticated caller never gets its body parsed", async () => {
  // The guarantee, not the mechanism: a body that is not JSON at all still answers 401, which it
  // could not do if the payload had been read first. /v1 settles the caller before the body too.
  const on = await up();
  const sent = await on.raw("POST", "/a2a/support", {
    rawBody: "{ this is not json",
  });
  expect(sent.status).toBe(401);
  expect(await faultName(sent)).toBe("InvalidRequestError");
});

test("a version this agent does not speak is refused before the body is read", async () => {
  const on = await up();
  const sent = await on.raw("POST", "/a2a/support", {
    version: "0.3",
    rawBody: "{ this is not json",
  });
  expect(await faultName(sent)).toBe("VersionNotSupportedError");
});

test("a host with no authenticate answers 401 on every principal route", async () => {
  const on = await serve({
    agents: { support: talker("hi") },
    a2a: { expose: ONE },
    withAuth: false,
  });
  const sent = await on.rpc("SendMessage", message("m1", "hi"), { as: alice });
  expect(sent.status).toBe(401);
});

test("a tenant that is not the caller's own is InvalidParamsError", async () => {
  const on = await up();
  const sent = await on.rpc(
    "SendMessage",
    { ...(message("m1", "hi") as object), tenant: "somebody-else" },
    { as: alice },
  );
  expect(await faultName(sent)).toBe("InvalidParamsError");
  // The caller's own tenant is accepted, so the refusal is about the mismatch and nothing else.
  const own = await on.rpc(
    "SendMessage",
    { ...(message("m2", "hi") as object), tenant: alice.tenant },
    { as: alice },
  );
  expect((await task(own)).id).toBeString();
});

test("a {tenant} path variant of an HTTP+JSON operation is 404", async () => {
  const on = await up();
  // The proto's additional_bindings put the tenant first; our card declares no interface tenant.
  const response = await on.raw("POST", "/a2a/acme/support/message:send", {
    as: alice,
    body: message("m1", "hi"),
  });
  expect(response.status).toBe(404);
  expect(await faultName(response)).toBe("MethodNotFoundError");
});

test("an agent that is not exposed is 404, exactly as a path that does not exist", async () => {
  const on = await serve({
    agents: { support: talker("hi"), helper: talker("hi") },
    a2a: { expose: ONE },
  });
  const hidden = await on.raw("POST", "/a2a/helper", {
    as: alice,
    body: { jsonrpc: "2.0", id: 1, method: "GetTask", params: { id: "x" } },
  });
  const absent = await on.raw("POST", "/a2a/nobody", {
    as: alice,
    body: { jsonrpc: "2.0", id: 1, method: "GetTask", params: { id: "x" } },
  });
  // The two answers are indistinguishable: a caller cannot probe which agents this host runs.
  const hiddenName = await faultName(hidden);
  expect(hiddenName).toBe(await faultName(absent));
  expect(hiddenName).toBe("MethodNotFoundError");
  expect(hidden.status).toBe(absent.status);
});

test("an unknown JSON-RPC method is MethodNotFoundError and echoes the request's id", async () => {
  const on = await up();
  const response = await on.raw("POST", "/a2a/support", {
    as: alice,
    body: { jsonrpc: "2.0", id: "abc", method: "Teleport", params: {} },
  });
  expect(await faultName(response.clone())).toBe("MethodNotFoundError");
  expect(await response.json()).toMatchObject({ id: "abc" });
});

test("an unmatched path under /a2a/ is MethodNotFoundError", async () => {
  const on = await up();
  const response = await on.http("GET", "/nothing/here", { as: alice });
  expect(response.status).toBe(404);
  expect(await faultName(response)).toBe("MethodNotFoundError");
});

test("a known operation with the wrong verb is MethodNotFoundError", async () => {
  const on = await up();
  // message:send is POST; a GET on it is not an operation we serve.
  const response = await on.http("GET", "/message:send", { as: alice });
  expect(await faultName(response)).toBe("MethodNotFoundError");
});

test("a file part is ContentTypeNotSupportedError, raw or by url", async () => {
  const on = await up();
  for (const part of [{ raw: "AAA" }, { url: "https://example/x.png" }]) {
    const response = await on.rpc(
      "SendMessage",
      {
        message: {
          messageId: `f-${JSON.stringify(part)}`,
          role: "ROLE_USER",
          parts: [part],
        },
      },
      { as: alice },
    );
    expect(await faultName(response)).toBe("ContentTypeNotSupportedError");
  }
});

test("a body that is not JSON is JSONParseError", async () => {
  const on = await up();
  const response = await on.host.fetch(
    new Request("http://host.test/a2a/support", {
      method: "POST",
      headers: {
        "A2A-Version": "1.0",
        "x-principal": JSON.stringify(alice),
        "content-type": "application/json",
      },
      body: "{not json",
    }),
  );
  expect(await faultName(response)).toBe("JSONParseError");
});

test("a body that is not a JSON-RPC 2.0 request is InvalidRequestError", async () => {
  const on = await up();
  const response = await on.raw("POST", "/a2a/support", {
    as: alice,
    body: { method: "GetTask" },
  });
  expect(await faultName(response)).toBe("InvalidRequestError");
});

test("both bindings answer the same task for the same message", async () => {
  const on = await up();
  const viaRpc = await task(
    await on.rpc("SendMessage", message("same", "hello"), { as: alice }),
  );
  const viaHttp = await task(
    await on.http("POST", "/message:send", {
      as: alice,
      body: message("same", "hello"),
    }),
  );
  expect(viaHttp.id).toBe(viaRpc.id);
  expect(viaHttp.contextId).toBe(viaRpc.contextId);
});

test("another tenant's principal gets its own task, never this tenant's", async () => {
  const on = await up();
  const mine = await task(
    await on.rpc("SendMessage", message("shared", "hello"), { as: alice }),
  );
  const theirs = await task(
    await on.rpc("SendMessage", message("shared", "hello"), { as: eve }),
  );
  expect(theirs.id).not.toBe(mine.id);
  // And neither can read the other's, because a task resolves only through its own receipts.
  const cross = await on.rpc("GetTask", { id: mine.id }, { as: eve });
  expect(await faultName(cross)).toBe("TaskNotFoundError");
});

test("an HTTP+JSON answer is application/a2a+json and a JSON-RPC answer is application/json", async () => {
  const on = await up();
  const http = await on.http("POST", "/message:send", {
    as: alice,
    body: message("ct-1", "hi"),
  });
  expect(http.headers.get("content-type")).toBe("application/a2a+json");
  const rpc = await on.rpc("SendMessage", message("ct-2", "hi"), { as: alice });
  expect(rpc.headers.get("content-type")).toBe("application/json");
});
