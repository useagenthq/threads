import { describe, expect, test } from "bun:test";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { knownEvents } from "../../src/reduce";
import { esc, refReader, render, verifyRequests } from "../../src/render";
import { fileArtifacts } from "../../src/store";
import { caseStore, loadCase } from "../conformance/cases";
import { unwrap } from "../store/helpers";

/** A case's log imported into a store holding its artifacts, as known events. */
function caseEvents(name: string) {
  const c = loadCase(name);
  const f = caseStore(c);
  const log = unwrap(f.store.importLog(c.log ?? new Uint8Array()));
  f.db.close();
  return { events: knownEvents(log), artifacts: f.artifacts };
}

const text = (bytes: Uint8Array): string => new TextDecoder().decode(bytes);

describe("golden: the declared prefix and tool schemas the model sees", () => {
  test("line 0 and a tools_changed line (deferred spec as a stub, then loaded)", () => {
    const { events, artifacts } = caseEvents("render-deferred-tool-loaded");
    const next = unwrap(render(events, refReader(artifacts)));
    const lines = text(next.bytes).split("\n");
    expect(text(next.prefix)).toMatchSnapshot();
    expect(
      lines.filter((l) => l.startsWith('{"role":"tools"')),
    ).toMatchSnapshot();
  });

  test("the compaction instruction line keeps the epoch's line 0", () => {
    const { events, artifacts } = caseEvents("compaction-summarizer-recorded");
    const next = unwrap(
      render(events, refReader(artifacts), { instruction: "SUMMARIZE" }),
    );
    const lines = text(next.bytes).trimEnd().split("\n");
    expect(lines[0]).toBe(text(next.prefix).trimEnd());
    expect(lines.at(-1)).toBe(
      '{"content":[{"text":"SUMMARIZE","type":"text"}],"role":"user"}',
    );
  });
});

describe("prefix stability (C7)", () => {
  test("every request of a thread declares the same line 0 as the next one", () => {
    const { events, artifacts } = caseEvents("prefix-stable-across-turns");
    const next = unwrap(render(events, refReader(artifacts)));
    const requests = events.flatMap((e) =>
      e.type === "model_request" ? [e.data.declared_prefix] : [],
    );
    expect(requests.length).toBeGreaterThan(1);
    for (const declared of requests)
      expect(declared.bytes).toBe(next.prefix.length);
    expect(verifyRequests(events, refReader(artifacts)).ok).toBe(true);
  });
});

describe("artifacts are verified on read", () => {
  test("a corrupt image artifact is artifact_corrupt at the event carrying it", () => {
    const { events, artifacts } = caseEvents("render-user-image-input");
    const root = mkdtempSync(join(tmpdir(), "threads-render-"));
    try {
      const files = fileArtifacts(root);
      const input = events.find((e) => e.type === "user_input");
      if (input?.type !== "user_input" || !("content" in input.data))
        throw new Error("the case starts with an image input");
      for (const part of input.data.content) {
        if (part.type !== "image_ref") continue;
        const sha = files.put(unwrap(artifacts.get(part.ref.sha256)));
        writeFileSync(join(root, "sha256", sha.slice(0, 2), sha), "tampered");
      }
      const result = render(events, refReader(files));
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
