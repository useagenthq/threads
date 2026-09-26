import { afterEach, describe, expect, test } from "bun:test";
import { agent, scriptedModel } from "@threads/core";
import {
  openStore,
  storeConnection,
  TeamId,
  type TeamItem,
  tenantStore,
} from "@threads/core/host";
import { z } from "zod";
import { rebuildTeamIndex } from "../../core/src/team/rebuild";
import { teamSse } from "../src/teams";
import { alice, eve, type Harness, harness, say, use } from "./kit";
import { sqlAll, sqlRun } from "./sql";

// GET /v1/teams/{team}/events (lane 29B): a lead team's feed as SSE. The route always follows,
// closes on epoch_restarted, and answers 401, 404 and 400 the same way as the Python host.

let h: Harness | undefined;
afterEach(async () => {
  await h?.host.stop();
  h = undefined;
});

const Accepted = z.object({ thread_id: z.string(), run_id: z.string() });

/** A lead behind the host that started a member; its team id. */
async function ranTeam(): Promise<string> {
  const researcher = agent({
    name: "researcher",
    model: scriptedModel({ responses: [say("Prices fell.")] }),
  });
  const lead = agent({
    name: "lead",
    model: scriptedModel({
      responses: [
        use("start", { agent: "researcher", task: "Go." }, "c1"),
        say("Started."),
        say("Prices fell."),
      ],
    }),
    team: [researcher],
  });
  h = harness({ agents: { lead } });
  const served = h;
  Accepted.parse(
    await (
      await served.call("POST", "/v1/runs", {
        as: alice,
        body: { agent: "lead", input: "Research prices." },
        headers: { "idempotency-key": "k-1" },
      })
    ).json(),
  );
  const { db } = await storeConnection(served.store);
  const [team] = z
    .array(z.object({ team_id: z.string() }))
    .parse(await sqlAll(db, "SELECT team_id FROM teams", []));
  if (team === undefined) throw new Error("a team");
  return team.team_id;
}

function readerOf(response: Response): ReadableStreamDefaultReader<Uint8Array> {
  const body = response.body;
  if (body === null) throw new Error("a body");
  return body.getReader();
}

/** `n` chunks of a following response, or all of them until it closes. */
async function chunks(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  n: number,
): Promise<string> {
  const utf8 = new TextDecoder();
  let text = "";
  for (let seen = 0; seen < n; seen += 1) {
    const next = await reader.read();
    if (next.done) break;
    text += utf8.decode(next.value, { stream: true });
  }
  return text;
}

/** `n` SSE blocks from a following response, then the reader lets go. */
async function blocks(response: Response, n: number): Promise<string[]> {
  const reader = readerOf(response);
  const utf8 = new TextDecoder();
  const found: string[] = [];
  let buffer = "";
  try {
    while (found.length < n) {
      const next = await reader.read();
      if (next.done) break;
      buffer += utf8.decode(next.value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop() ?? "";
      found.push(...parts);
    }
  } finally {
    await reader.cancel();
  }
  return found.slice(0, n);
}

const dataOf = (block: string): unknown =>
  JSON.parse(block.slice(block.indexOf("data: ") + 6));
const idOf = (block: string): string =>
  block.slice(block.indexOf("id: ") + 4, block.indexOf("\n"));

describe("the lead team's event stream", () => {
  test("each item is one message: id <epoch>:<offset> and the item as canonical JSON", async () => {
    const team = await ranTeam();
    const served = h;
    if (served === undefined) throw new Error("a host");
    const response = await served.call("GET", `/v1/teams/${team}/events`, {
      as: alice,
    });
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("text/event-stream");
    const [first, second] = await blocks(response, 2);
    if (first === undefined || second === undefined)
      throw new Error("two messages");
    expect(idOf(first)).toBe("1:1");
    expect(idOf(second)).toBe("1:2");
    // RFC 8785: keys in code-point order, and no spaces.
    expect(first).toContain('data: {"cursor":{"epoch":1,"offset":1},');
    const item = z
      .object({ kind: z.string(), source: z.object({ kind: z.string() }) })
      .parse(dataOf(first));
    expect(item).toMatchObject({ kind: "event", source: { kind: "team" } });
  });

  test("an unauthenticated read is 401, and another tenant's team is 404", async () => {
    const team = await ranTeam();
    const served = h;
    if (served === undefined) throw new Error("a host");
    const anonymous = await served.call("GET", `/v1/teams/${team}/events`);
    expect(anonymous.status).toBe(401);
    // Its tenant is acme: eve's tenant has no such team.
    const other = await served.call("GET", `/v1/teams/${team}/events`, {
      as: eve,
    });
    expect(other.status).toBe(404);
  });

  test("the host team's id is not found: it has no HTTP stream", async () => {
    // A tenant whose only team is a host team (Phase 2, lane 29D): reachable only through
    // Host.team, never over HTTP. Nothing runs here, so nothing else reads the row.
    h = harness({
      agents: {
        lead: agent({
          name: "lead",
          model: scriptedModel({ responses: [say("Ready.")] }),
        }),
      },
    });
    const served = h;
    const id = "0192c000-0000-7000-8000-0000000000aa";
    const { log } = await openStore(tenantStore(served.store, "acme"));
    await sqlRun(
      log.driver,
      `INSERT INTO teams (team_id, tenant_id, kind, lead_thread_id, team_log_branch_id, closed_at)
         VALUES (?, 'acme', 'host', NULL, ?, NULL)`,
      [id, "0192c000-0000-7000-8000-0000000000ab"],
    );
    const response = await served.call("GET", `/v1/teams/${id}/events`, {
      as: alice,
    });
    expect(response.status).toBe(404);
    expect(await response.json()).toMatchObject({
      error: { code: "not_found" },
    });
    // An id no team has is the same answer, so the host team is not distinguishable.
    const unknown = await served.call(
      "GET",
      "/v1/teams/0192c000-0000-7000-8000-0000000000ff/events",
      { as: alice },
    );
    expect(unknown.status).toBe(404);
  });

  test("a malformed or future cursor is 400 invalid_cursor, and Last-Event-ID wins over ?after", async () => {
    const team = await ranTeam();
    const served = h;
    if (served === undefined) throw new Error("a host");
    for (const after of ["nonsense", "1:x", "1:2:3", "1:", " 1:2", ""])
      expect(
        (
          await served.call(
            "GET",
            `/v1/teams/${team}/events?after=${encodeURIComponent(after)}`,
            { as: alice },
          )
        ).status,
      ).toBe(after === "" ? 200 : 400);
    // A later epoch than the feed's: refused, not restarted.
    const future = await served.call(
      "GET",
      `/v1/teams/${team}/events?after=9:0`,
      { as: alice },
    );
    expect(future.status).toBe(400);
    expect(await future.json()).toMatchObject({
      error: { code: "invalid_cursor" },
    });
    // Last-Event-ID wins: a good header beats a bad query, and a bad header beats a good query.
    const header = await served.call(
      "GET",
      `/v1/teams/${team}/events?after=nonsense`,
      { as: alice, headers: { "last-event-id": "1:2" } },
    );
    expect(header.status).toBe(200);
    const [first] = await blocks(header, 1);
    expect(first === undefined ? "" : idOf(first)).toBe("1:3");
    const beaten = await served.call(
      "GET",
      `/v1/teams/${team}/events?after=1:2`,
      {
        as: alice,
        headers: { "last-event-id": "9:0" },
      },
    );
    expect(beaten.status).toBe(400);
  });

  test("an index wipe mid-follow sends one epoch_restarted and closes; the new cursor resumes", async () => {
    const team = await ranTeam();
    const served = h;
    if (served === undefined) throw new Error("a host");
    const { log } = await openStore(tenantStore(served.store, "acme"));
    const committed = z
      .array(z.object({ n: z.number() }))
      .parse(
        await sqlAll(log.driver, "SELECT COUNT(*) AS n FROM team_feed", []),
      )[0]?.n;
    if (committed === undefined) throw new Error("a feed");
    const response = await served.call("GET", `/v1/teams/${team}/events`, {
      as: alice,
    });
    const reader = readerOf(response);
    // Read the whole of epoch 1, wipe the index, then read to the close.
    let text = await chunks(reader, committed);
    const rebuilt = await rebuildTeamIndex(log, TeamId.parse(team));
    if (!rebuilt.ok) throw new Error(rebuilt.error.message);
    text += await chunks(reader, Number.POSITIVE_INFINITY);
    const messages = text.split("\n\n").filter((b) => b.includes("data: "));
    const last = messages.at(-1);
    if (last === undefined) throw new Error("a message");
    expect(dataOf(last)).toEqual({
      kind: "epoch_restarted",
      cursor: { epoch: 2, offset: 0 },
    });
    expect(idOf(last)).toBe("2:0");
    expect(messages.filter((m) => m.includes("epoch_restarted"))).toHaveLength(
      1,
    );
    // The client reconnects with the new cursor and resumes in the new epoch.
    const again = await served.call("GET", `/v1/teams/${team}/events`, {
      as: alice,
      headers: { "last-event-id": "2:0" },
    });
    const [first] = await blocks(again, 1);
    expect(first === undefined ? "" : idOf(first)).toBe("2:1");
  });
});

describe("the team stream's keepalive", () => {
  test("a quiet stream sends ': keepalive' comments, which move no cursor", async () => {
    // The one item waits until the test has seen two beats, so no clock decides the outcome.
    const release = Promise.withResolvers<void>();
    const restarted: TeamItem = {
      kind: "epoch_restarted",
      cursor: { epoch: 1, offset: 0 },
    };
    async function* quiet(): AsyncGenerator<TeamItem> {
      await release.promise;
      yield restarted;
    }
    const reader = teamSse(quiet(), 1).getReader();
    const utf8 = new TextDecoder();
    let beats = 0;
    let last = "";
    for (;;) {
      const next = await reader.read();
      if (next.done) break;
      last = utf8.decode(next.value);
      if (last === ": keepalive\n\n") {
        beats += 1;
        if (beats === 2) release.resolve();
        continue;
      }
      break;
    }
    expect(beats).toBe(2);
    // The beats moved no cursor: the item after them is still the first one.
    expect(last).toBe(
      'id: 1:0\ndata: {"cursor":{"epoch":1,"offset":0},"kind":"epoch_restarted"}\n\n',
    );
  });
});
