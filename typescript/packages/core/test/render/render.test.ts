import { describe, expect, test } from "bun:test";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { knownEvents } from "../../src/reduce";
import { esc, renderFrom, verifyRequests } from "../../src/render";
import { fileArtifacts } from "../../src/store";
import { caseStore, loadCase } from "../conformance/cases";
import { unwrap } from "../store/helpers";

/** A case's log imported into a store holding its artifacts, as known events. */
async function caseEvents(name: string) {
  const c = loadCase(name);
  const f = await caseStore(c);
  const log = unwrap(await f.store.importLog(c.log ?? new Uint8Array()));
  await f.db.close();
  return { events: knownEvents(log), artifacts: f.artifacts };
}

const text = (bytes: Uint8Array): string => new TextDecoder().decode(bytes);

describe("golden: the declared prefix and tool schemas the model sees", () => {
  test("line 0 and a tools_loaded line (deferred spec as a stub, then loaded)", async () => {
    const { events, artifacts } = await caseEvents(
      "render-deferred-tool-loaded",
    );
    const next = unwrap(await renderFrom(events, artifacts));
    const lines = text(next.bytes).split("\n");
    expect(text(next.prefix)).toMatchSnapshot();
    expect(
      lines.filter((l) => l.startsWith('{"role":"tools')),
    ).toMatchSnapshot();
  });

  test("the compaction instruction line keeps the epoch's line 0", async () => {
    const { events, artifacts } = await caseEvents(
      "compaction-summarizer-recorded",
    );
    const next = unwrap(
      await renderFrom(events, artifacts, { instruction: "SUMMARIZE" }),
    );
    const lines = text(next.bytes).trimEnd().split("\n");
    expect(lines[0]).toBe(text(next.prefix).trimEnd());
    expect(lines.at(-1)).toBe(
      '{"content":[{"text":"SUMMARIZE","type":"text"}],"role":"user"}',
    );
  });
});

describe("prefix stability (C7)", () => {
  test("every request of a thread declares the same line 0 as the next one", async () => {
    const { events, artifacts } = await caseEvents(
      "prefix-stable-across-turns",
    );
    const next = unwrap(await renderFrom(events, artifacts));
    const requests = events.flatMap((e) =>
      e.type === "model_request" ? [e.data.declared_prefix] : [],
    );
    expect(requests.length).toBeGreaterThan(1);
    for (const declared of requests)
      expect(declared.bytes).toBe(next.prefix.length);
    expect((await verifyRequests(events, artifacts)).ok).toBe(true);
  });
});

describe("artifacts are verified on read", () => {
  test("a corrupt image artifact is artifact_corrupt at the event carrying it", async () => {
    const { events, artifacts } = await caseEvents("render-user-image-input");
    const root = mkdtempSync(join(tmpdir(), "threads-render-"));
    try {
      const files = fileArtifacts(root);
      const input = events.find((e) => e.type === "user_input");
      if (input?.type !== "user_input" || !("content" in input.data))
        throw new Error("the case starts with an image input");
      for (const part of input.data.content) {
        if (part.type !== "image_ref") continue;
        const sha = await files.put(
          unwrap(await artifacts.get(part.ref.sha256)),
        );
        writeFileSync(join(root, "sha256", sha.slice(0, 2), sha), "tampered");
      }
      const result = await renderFrom(events, files);
      expect(result.ok ? "ok" : result.error).toMatchObject({
        code: "artifact_corrupt",
        seq: input.seq,
      });
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});

test("esc escapes & first, then < > \" '", () => {
  expect(esc(`</reference><x a="1" b='2'>&amp;`)).toBe(
    "&lt;/reference&gt;&lt;x a=&quot;1&quot; b=&#39;2&#39;&gt;&amp;amp;",
  );
});
