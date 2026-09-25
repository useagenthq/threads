import { expect, test } from "bun:test";
import { BranchId, type EventId, ThreadId } from "../../src/log";
import type { EventDraft } from "../../src/store";
import { treeCost } from "../../src/thread/cost-tree";
import { code, fixture, started, unwrap } from "../store/helpers";

// Thread.cost({ tree: true }) walks descendants at any depth (spec/api.json): a chain of 10,000
// subagents is walked with an explicit stack, never the call stack.

const DEPTH = 10_000;
const hex = (i: number) => i.toString(16).padStart(12, "0");
const threadId = (i: number) =>
  ThreadId.parse(`0192a000-0000-7000-8000-${hex(i)}`);
const branchId = (i: number) =>
  BranchId.parse(`0192b000-0000-7000-8000-${hex(i)}`);

type Link = {
  readonly thread_id: ThreadId;
  readonly branch_id: BranchId;
  readonly event_id: EventId;
  readonly relation: "subagent";
};

function spawned(child: ThreadId): EventDraft {
  return {
    type: "agent_spawned",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: {
      call_id: `s-${child}`,
      child_thread_id: child,
      agent_name: "kid",
      mode: "foreground",
      isolation: "none",
    },
  };
}

/** Thread i's log: its thread_started (a child names the spawn that started it), then a spawn. */
async function append(i: number, parent: Link | undefined): Promise<Link> {
  const { store } = deep;
  unwrap(await store.createBranch(threadId(i), branchId(i)));
  const writer = unwrap(await store.acquire(branchId(i), "deep-test"));
  // The deepest thread spawns the root again: the walk only meets it by reaching the bottom.
  const child = threadId(i + 1 < DEPTH ? i + 1 : 0);
  const first: EventDraft =
    parent === undefined || started.type !== "thread_started"
      ? started
      : { ...started, data: { ...started.data, parent } };
  const [, spawn] = unwrap(await writer.append([first, spawned(child)]));
  if (spawn === undefined) throw new Error("agent_spawned was not appended");
  return {
    thread_id: threadId(i),
    branch_id: branchId(i),
    event_id: spawn.event.event_id,
    relation: "subagent",
  };
}

const deep = await fixture();

test("a chain of 10,000 subagents is walked to the bottom without recursion", async () => {
  let parent: Link | undefined;
  for (let i = 0; i < DEPTH; i++) parent = await append(i, parent);
  const { store } = deep;
  const total = await treeCost(
    store,
    threadId(0),
    unwrap(await store.read(branchId(0))),
  );
  expect(code(total)).toBe("log_corrupt");
  // The path names every thread down to the bottom one.
  expect(total.ok ? "" : total.error.message).toEndWith(
    `child ${threadId(DEPTH - 1)}: child ${threadId(0)}: thread ${threadId(0)} appears twice in the tree`,
  );
}, 300_000);
