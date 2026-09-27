import { expect, test } from "bun:test";
import { resume } from "../../../core/src/loop";
import { events } from "../../../core/test/loop/harness";
import { ROOT, unwrap, userInput } from "../../../core/test/store/helpers";
import { bearer, remote } from "../../src";
import { calls, drill, lastOf, TOOL, types } from "./drill";
import { CARD_URL, partner, task } from "./partner";

// What the two tools declare, and the refusals that happen before any byte leaves. The declared
// contract is asserted here rather than only through behaviour: a send whose finality flipped to
// "final" would re-dispatch under the same key, which is the one thing invariant 3 forbids.

test("the send is reconcilable and nonfinal; the status read is read_only", async () => {
  const r = remote("refunds", CARD_URL, { transport: partner() });
  const tools = r.tools({ name: TOOL, description: "Ask the desk." });
  expect(tools.map((t) => t.name)).toEqual([TOOL, `${TOOL}_status`]);
  const [send, status] = tools.map((t) => t.spec());
  expect(send?.effect_class).toBe("reconcilable");
  // Not idempotent, and no window: the window comes from a partner's card, which is unknown here.
  expect(send?.dedup_window_ms).toBeUndefined();
  expect(status?.effect_class).toBe("read_only");
  const env = {
    deps: undefined,
    threadId: "0192a000-0000-7000-8000-000000000001",
    branchId: ROOT,
    principal: { issuer: "api", tenant: "acme", subject: "alice" },
  };
  const impl = tools[0]?.bind(env);
  expect(impl?.reconcile?.finality).toBe("nonfinal");
  // The status read has no reconcile contract because it begins no effect.
  expect(tools[1]?.bind(env).reconcile).toBeUndefined();
});

test("a task this conversation did not create is refused before anything is sent", async () => {
  const p = partner();
  // The model names a task id of its own invention.
  const h = await drill(p, {
    responses: [
      calls({ message: "what about this one?", task_id: "someone-elses" }),
    ],
  });
  p.send = () => {
    throw new Error("a refused task id never reaches a send");
  };
  const w = unwrap(await h.store.acquire(ROOT, "owner", 30_000));
  await resume(w, h.artifacts, h.config(), { input: userInput("ask") });
  // Nothing began, nothing was stored, and nothing was sent.
  expect(types(events(w))).not.toContain("remote_call");
  expect(types(events(w))).not.toContain("effect_begin");
  expect(lastOf(events(w), "tool_result")?.data).toMatchObject({
    is_error: true,
    origin: "not_executed",
  });
  expect(lastOf(events(w), "tool_result")?.data.preview).toContain(
    "unknown_task",
  );
  expect(p.sends()).toHaveLength(0);
});

test("the credential rides in the header and reaches no stored byte", async () => {
  const p = partner();
  const token = "s3cret-partner-token";
  const h = await drill(p, { auth: bearer(token) });
  p.send = () => ({
    kind: "task",
    task: task("task-1", "TASK_STATE_COMPLETED", "paid"),
  });
  const w = unwrap(await h.store.acquire(ROOT, "owner", 30_000));
  await resume(w, h.artifacts, h.config(), { input: userInput("ask") });
  const send = p.sends()[0];
  expect(send?.headers["authorization"]).toBe(`Bearer ${token}`);
  // The stored request bytes are what a re-dispatch replays, so the token must not be in them.
  expect(send?.body ?? "").not.toContain(token);
  const stored = events(w).flatMap((e) =>
    e.type === "remote_call" ? [e.data.request_ref.sha256] : [],
  );
  const bytes = await h.artifacts.get(stored[0] ?? "");
  expect(new TextDecoder().decode(unwrap(bytes))).not.toContain(token);
});

test("the opaque provenance claim carries no principal, tenant or subject", async () => {
  const p = partner();
  const h = await drill(p);
  p.send = () => ({
    kind: "task",
    task: task("task-1", "TASK_STATE_COMPLETED", "paid"),
  });
  const w = unwrap(await h.store.acquire(ROOT, "owner", 30_000));
  await resume(w, h.artifacts, h.config(), { input: userInput("ask") });
  const body = p.sends()[0]?.body ?? "";
  expect(body).toContain("https://threadsai.dev/a2a/provenance/v1");
  expect(body).toContain('"hops":1');
  for (const secret of ["acme", "alice", "api"])
    expect(body).not.toContain(secret);
});
