import { afterEach, describe, expect, setSystemTime, test } from "bun:test";
import { z } from "zod";
import { hostTicked } from "../src/host";
import {
  alice,
  bob,
  type Harness,
  harness,
  knownEventsOf,
  mailer,
  say,
  until,
  use,
} from "./kit";

// answerQuestion over the host API: a run started by an authenticated call pins ask_user, and
// only its asker answers, strictly to the question's options (invalid_answer otherwise).

let h: Harness | undefined;
afterEach(async () => {
  await h?.host.stop();
  h = undefined;
  setSystemTime();
});

const Accepted = z.object({ thread_id: z.string(), branch_id: z.string() });
const Failed = z.object({ error: z.object({ code: z.string() }) });

async function asked(harnessed: Harness) {
  const accepted = Accepted.parse(
    await (
      await harnessed.call("POST", "/v1/runs", {
        as: alice,
        body: { agent: "support", input: "Paint it." },
        headers: { "idempotency-key": "k-1" },
      })
    ).json(),
  );
  const events = () =>
    knownEventsOf(harnessed.store, "acme", accepted.branch_id);
  await until(async () => (await events()).some((e) => e.type === "parked"));
  const answer = (as: typeof alice, text: string) =>
    harnessed.call(
      "POST",
      `/v1/threads/${accepted.thread_id}/questions/q1/answer`,
      { as, body: { answer: text } },
    );
  return { events, answer };
}

describe("answerQuestion", () => {
  test("an answer racing the expiry: exactly one result and one resumed", async () => {
    h = harness({
      agents: {
        support: mailer({
          responses: [
            use("ask_user", { question: "Which?" }, "q1"),
            say("Ok."),
          ],
        }),
      },
    });
    const { events, answer } = await asked(h);
    await h.host.ready();
    setSystemTime(new Date(Date.now() + 25 * 3_600_000));
    await Promise.all([answer(alice, "red"), hostTicked(h.host)]);
    await until(async () =>
      (await events()).some((e) => e.type === "turn_completed"),
    );
    const log = await events();
    const settled = log.filter(
      (e) => e.type === "tool_result" && e.data.call_id === "q1",
    );
    expect(settled).toHaveLength(1);
    expect(log.filter((e) => e.type === "resumed")).toHaveLength(1);
  });

  test("forbidden to anyone but the asker; a non-option is invalid_answer and the question stays open", async () => {
    h = harness({
      agents: {
        support: mailer({
          responses: [
            use(
              "ask_user",
              { question: "Which?", options: ["red", "blue"] },
              "q1",
            ),
            say("Red."),
          ],
        }),
      },
    });
    const { events, answer } = await asked(h);
    const byBob = await answer(bob, "red");
    expect(byBob.status).toBe(403);
    const wrong = await answer(alice, "green");
    expect(wrong.status).toBe(409);
    expect(Failed.parse(await wrong.json()).error.code).toBe("invalid_answer");
    expect((await events()).at(-1)?.type).toBe("parked");
    expect((await answer(alice, " RED ")).status).toBe(200);
    await until(async () =>
      (await events()).some((e) => e.type === "turn_completed"),
    );
    const again = await answer(alice, "red");
    expect(Failed.parse(await again.json()).error.code).toBe(
      "no_open_question",
    );
    const answered = (await events()).find(
      (e) => e.type === "tool_result" && e.data.origin === "answered",
    );
    expect(answered?.type === "tool_result" && answered.data.preview).toBe(
      "red",
    );
  });
});
