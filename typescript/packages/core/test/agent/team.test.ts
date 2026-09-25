import { describe, expect, test } from "bun:test";
import { agent, scriptedModel, sqlite, type ThreadRef } from "../../src";
import { openStore } from "../../src/agent/sqlite";
import { CallId, EventId, type KnownEvent } from "../../src/log";
import { teamOf } from "../../src/loop/agents/team";
import { Session } from "../../src/loop/session";
import { knownEvents, projections } from "../../src/reduce";
import { harness } from "../loop/harness";
import { ROOT, unwrap } from "../store/helpers";

// Teams (F7.3, F7.4, F7.11): the lead's log holds the tasks and the mailbox;
// members act only through the lead's writer, so a claim is atomic and a repeated call (a
// member re-dispatching after a restart) never claims or sends twice.

const usage = { input_tokens: 10, output_tokens: 2 };
const say = (text: string) => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage,
});
const use = (name: string, input: Record<string, unknown>, id: string) => ({
  content: [{ type: "tool_use", call_id: id, name, input }],
  stop_reason: "tool_use",
  usage,
});

type Store = ReturnType<typeof sqlite>;

async function read(store: Store, thread: ThreadRef) {
  const { log } = await openStore(store);
  return unwrap(await log.read(thread.branch));
}

async function childLogs(
  store: Store,
  parent: readonly KnownEvent[],
): Promise<readonly (readonly KnownEvent[])[]> {
  const { log } = await openStore(store);
  const children: (readonly KnownEvent[])[] = [];
  for (const e of parent) {
    if (e.type !== "agent_spawned") continue;
    const branch = unwrap(await log.mainBranch(e.data.child_thread_id));
    children.push(knownEvents(unwrap(await log.read(branch))));
  }
  return children;
}

describe("a team: shared tasks and addressed messages", () => {
  test("members claim from the lead's list, message each other, and blockers gate claims", async () => {
    const store = sqlite(":memory:");
    const alice = agent({
      name: "alice",
      model: scriptedModel({
        responses: [
          use("team_task_claim", { task_id: "lead/c1" }, "a1"),
          use("send_message", { to: "*", text: "Schema is up." }, "a2"),
          use(
            "team_task_update",
            { task_id: "lead/c1", status: "completed" },
            "a3",
          ),
          say("Schema done."),
        ],
      }),
    });
    // bob runs twice: before alice (the runner is blocked) and after her (it isn't).
    const bob = agent({
      name: "bob",
      model: scriptedModel({
        responses: [
          use("team_task_claim", { task_id: "lead/c2" }, "b1"),
          say("Blocked."),
          use("team_task_claim", { task_id: "lead/c1" }, "b2"),
          use("team_task_claim", { task_id: "lead/c2" }, "b3"),
          say("Runner claimed."),
        ],
      }),
    });
    const lead = agent({
      name: "lead",
      model: scriptedModel({
        responses: [
          use("team_task_create", { subject: "Write the schema" }, "c1"),
          use(
            "team_task_create",
            { subject: "Write the runner", blocked_by: ["lead/c1"] },
            "c2",
          ),
          use(
            "spawn_agent",
            { agent: "bob", prompt: "Claim the runner early." },
            "c3",
          ),
          use(
            "spawn_agent",
            { agent: "alice", prompt: "Do the schema." },
            "c4",
          ),
          use(
            "spawn_agent",
            { agent: "bob", prompt: "Take the runner." },
            "c5",
          ),
          say("Team is working."),
        ],
      }),
      subagents: [alice, bob],
    });
    const result = await lead.run("Build it.", { store });
    expect(result.status).toBe("completed");
    const chain = await read(store, result.thread);
    expect(projections(chain).team_tasks).toEqual([
      { task_id: "lead/c1", status: "completed", owner: "alice" },
      { task_id: "lead/c2", status: "claimed", owner: "bob" },
    ]);
    const log = knownEvents(chain);
    const messages = log.flatMap((e) =>
      e.type === "team_message" ? [e.data] : [],
    );
    expect(messages).toEqual([
      { message_id: "alice/a2", from: "alice", to: "*", text: "Schema is up." },
    ]);
    const [bobEarly, , bobLate] = await childLogs(store, log);
    const results = (child: readonly KnownEvent[] | undefined) =>
      (child ?? []).flatMap((e) =>
        e.type === "tool_result" ? [[e.data.call_id, e.data.is_error]] : [],
      );
    // A blocked task can't be claimed; a completed one can't be claimed again.
    expect<unknown>(results(bobEarly)).toEqual([["b1", true]]);
    expect<unknown>(results(bobLate)).toEqual([
      ["b2", true],
      ["b3", false],
    ]);
    // The second bob thread saw alice's broadcast before its first request, as untrusted reference.
    const injected = (bobLate ?? []).find((e) => e.type === "injected");
    expect(injected?.type === "injected" && injected.data).toEqual({
      source: "agent",
      trust: "untrusted_reference",
      origin: { id: "alice/a2" },
      text: "alice: Schema is up.",
    });
  });
});

describe("team acts are idempotent per member call (F7.4)", () => {
  test("a repeated claim, update or message appends nothing and answers the same", async () => {
    const h = await harness([], [], []);
    const writer = unwrap(await h.store.acquire(ROOT, "lead"));
    const agents = {
      name: "lead",
      subagent: () => undefined,
      subagents: ["alice", "bob"],
    };
    const s = new Session(writer, h.artifacts, h.config({ agents }));
    const team = teamOf(s);
    const call = (name: string, input: Record<string, string>, id: string) => ({
      call_id: CallId.parse(id),
      name,
      input,
      request_event_id: EventId.parse("0192a000-0000-7000-8000-000000000001"),
    });
    const create = call("team_task_create", { subject: "Schema" }, "c1");
    const claim = call("team_task_claim", { task_id: "lead/c1" }, "c2");
    const send = call("send_message", { to: "bob", text: "Mine." }, "c3");
    const first = [
      await team.act("lead", create),
      await team.act("alice", claim),
      await team.act("alice", send),
    ];
    const seq = s.fold.seq;
    const again = [
      await team.act("lead", create),
      await team.act("alice", claim),
      await team.act("alice", send),
    ];
    expect(again).toEqual(first);
    expect(first.map((a) => a.isError)).toEqual([false, false, false]);
    expect(s.fold.seq).toBe(seq);
    // A name that is not on the team is refused and appends nothing.
    const stray = call("send_message", { to: "billing", text: "Hi." }, "c4");
    expect(await team.act("alice", stray)).toEqual({
      isError: true,
      output:
        "unknown_recipient: billing is not on this team; send to lead, alice, bob or * for everyone",
    });
    expect(s.fold.seq).toBe(seq);
    expect(
      await team.act(
        "bob",
        call("team_task_claim", { task_id: "lead/c1" }, "c9"),
      ),
    ).toEqual({
      isError: true,
      output: "can't claim: team_task_claimed for lead/c1, which is not open",
    });
  });
});
