import { describe, expect, test } from "bun:test";
import { agent, ConfigError, scriptedModel, sqlite } from "../../src";
import { openStore } from "../../src/agent/sqlite";
import type { EventDraft } from "../../src/store";
import { logOf } from "../agent/kit";
import { caseStore, loadCase } from "../conformance/cases";
import { code, unwrap } from "../store/helpers";
import {
  events,
  finished,
  last,
  operator,
  requestText,
  SUMMARY,
  say,
} from "./methods-kit";

// agent({outputStyles}) and Thread.setOutputStyle (spec/api.json; ADR 0020 item 1): a pinned
// instruction appended after the prompt prefix, so line 0 and the cache are kept.

const STYLES = { concise: "Answer in at most three sentences." };
// The config_hash this agent pinned before output styles existed (fallback.test.ts pins it too).
const PLAIN_HASH =
  "118df2e5df0d26f55795c55185e95d4b82bf0060d21b6c3653abee6bc35f1e8f";

const plain = (outputStyles?: Readonly<Record<string, string>>) =>
  agent({
    name: "support",
    instructions: "Help the user.",
    model: scriptedModel({ responses: [say("one"), say("two")] }),
    ...(outputStyles === undefined ? {} : { outputStyles }),
  });

describe("agent outputStyles", () => {
  test("pins policy.output_styles; an agent without styles pins none and keeps its hash", async () => {
    const styled = await plain(STYLES).run("hi", { store: sqlite(":memory:") });
    const pinned = (await logOf(styled.thread))[0];
    if (pinned?.type !== "thread_started") throw new Error("thread_started");
    expect(pinned.data.policy?.output_styles).toEqual(STYLES);
    expect(pinned.data.config_hash).not.toBe(PLAIN_HASH);
    const bare = await plain().run("hi", { store: sqlite(":memory:") });
    const unstyled = (await logOf(bare.thread))[0];
    if (unstyled?.type !== "thread_started") throw new Error("thread_started");
    expect(unstyled.data.policy?.output_styles).toBeUndefined();
    expect(unstyled.data.config_hash).toBe(PLAIN_HASH);
  });

  test("an empty name or text is invalid_config naming the style", async () => {
    for (const styles of [{ "": "x" }, { concise: "" }]) {
      const checked = await plain(styles).check();
      expect(checked).toMatchObject({
        ok: false,
        error: { code: "invalid_config" },
      });
      if (!checked.ok) expect(checked.error.message).toContain("outputStyles");
    }
    // Untyped config, as from a JSON file: the check still refuses a non-string text.
    const untyped: Record<string, string> = JSON.parse('{"concise": 3}');
    const run = plain(untyped).run("hi", { store: sqlite(":memory:") });
    await expect(run).rejects.toBeInstanceOf(ConfigError);
  });

  test("adding a style to a running thread's agent fails closed", async () => {
    const first = await plain().run("hi", { store: sqlite(":memory:") });
    await expect(
      plain(STYLES).run("more", { thread: first.thread }),
    ).rejects.toBeInstanceOf(ConfigError);
  });
});

describe("Thread.setOutputStyle", () => {
  test("appends the pinned text; line 0 and the earlier request bytes are kept", async () => {
    const { bot, ref, thread } = await finished([say("Hi."), say("Brief.")], {
      outputStyles: STYLES,
    });
    const set = unwrap(await thread.setOutputStyle("concise", operator));
    const style = last(await events(ref), "injected");
    expect(style.event_id).toBe(set.event_id);
    expect(style.actor).toEqual({ kind: "user", principal: operator });
    expect(style.data).toEqual({
      source: "output_style",
      trust: "trusted_instruction",
      origin: { id: "concise" },
      text: STYLES.concise,
    });
    await bot.run("next", { store: ref.store, thread: ref });
    const [before, after] = (await events(ref)).filter(
      (e) => e.type === "model_request",
    );
    if (before?.type !== "model_request" || after?.type !== "model_request")
      throw new Error("two requests");
    // C7 within one epoch: the declared prefix is byte-equal, not just a starting match.
    expect(after.data.declared_prefix).toEqual(before.data.declared_prefix);
    const old = await requestText(ref.store, before);
    const next = await requestText(ref.store, after);
    expect(next.startsWith(old)).toBe(true);
    expect(next).toContain(
      '<context source=\\"output_style\\" id=\\"concise\\">\\nAnswer in at most three sentences.\\n</context>',
    );
    expect(unwrap(await thread.replay())).toBeUndefined();
  });

  test("an unknown name is not_found listing the styles; a thread with none says so", async () => {
    const styled = await finished([say("Hi.")], { outputStyles: STYLES });
    expect(await styled.thread.setOutputStyle("loud", operator)).toMatchObject({
      ok: false,
      error: {
        code: "not_found",
        message: "no output style loud; the agent defines concise",
      },
    });
    const bare = await finished([say("Hi.")]);
    expect(await bare.thread.setOutputStyle("concise", operator)).toMatchObject(
      {
        ok: false,
        error: { message: "no output style concise; the agent defines none" },
      },
    );
  });

  test("a compaction that drops the style restores it with the summary, in one batch", async () => {
    const { bot, ref, thread } = await finished(
      [say("Hi."), say(SUMMARY), say("Brief.")],
      { outputStyles: STYLES },
    );
    unwrap(await thread.setOutputStyle("concise", operator));
    unwrap(await thread.compact(operator));
    await bot.run("next", { store: ref.store, thread: ref });
    const log = await events(ref);
    const compacted = last(log, "compacted");
    const after = log[log.indexOf(compacted) + 1];
    expect(after).toMatchObject({
      type: "injected",
      actor: { kind: "host" },
      data: { source: "output_style", text: STYLES.concise },
    });
    const request = log.findLast((e) => e.type === "model_request");
    if (request === undefined) throw new Error("a turn request");
    expect(await requestText(ref.store, request)).toContain("output_style");
  });
});

describe("rule 29 at the boundary", () => {
  const style = (
    text: string,
    kind: "user" | "model" | "host",
    id = "concise",
  ): EventDraft => ({
    type: "injected",
    type_version: 1,
    critical: true,
    actor: kind === "user" ? { kind, principal: operator } : { kind },
    data: {
      source: "output_style",
      trust: "trusted_instruction",
      origin: { id },
      text,
    },
  });

  test("the writer refuses an invented text, an unknown name and a model-set style", async () => {
    const { ref } = await finished([say("Hi.")], { outputStyles: STYLES });
    const { log } = await openStore(ref.store);
    const writer = unwrap(await log.acquire(ref.branch, "test"));
    try {
      for (const bad of [
        style("Shout.", "user"),
        style(STYLES.concise, "user", "loud"),
        style(STYLES.concise, "model"),
        // The host re-appends a style only as the restore right after a compaction.
        style(STYLES.concise, "host"),
      ])
        expect(code(await writer.append([bad]))).toBe("invalid_transition");
      expect(code(await writer.append([style(STYLES.concise, "user")]))).toBe(
        "ok",
      );
    } finally {
      await writer.release();
    }
  });

  test("import refuses a log with one", async () => {
    for (const name of [
      "output-style-text-mismatch-rejected",
      "output-style-unknown-name-rejected",
      "output-style-by-model-rejected",
      "output-style-host-orphan-rejected",
      "output-style-host-open-turn-rejected",
      "output-style-host-late-rejected",
    ]) {
      const c = loadCase(name);
      const imported = (await caseStore(c)).store.importLog(
        c.log ?? new Uint8Array(),
      );
      expect(code(await imported)).toBe("invalid_transition");
    }
  });
});
