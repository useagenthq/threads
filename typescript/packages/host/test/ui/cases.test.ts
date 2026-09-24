import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { CASE_NAMES, loadCase } from "../../../core/test/conformance/cases";
import { line, runUiCase } from "./case-kit";
import {
  agUiFold,
  agUiProblems,
  agUiProjection,
  aiSdkFold,
  aiSdkProblems,
  aiSdkProjection,
  threadsFold,
} from "./stock";

// Every `ui` case of spec/conformance/cases, as spec/conformance/README.md "ui" runs it: the
// host's frames byte for byte, each valid under its pinned schema, the stream accepted by the
// stock client's own checks, and the messages that client builds.

const Expected = z.object({ messages: z.array(z.unknown()) });

for (const name of CASE_NAMES) {
  const c = loadCase(name);
  if (c.kind !== "ui") continue;
  describe(name, () => {
    test("frames, schemas, the stock client's checks and messages", async () => {
      const run = await runUiCase(c);
      const lines = run.frames.map(line);
      if (run.done && run.protocol === "ai-sdk") lines.push(line("[DONE]"));
      const want = readFileSync(join(c.dir, "frames.jsonl"), "utf8").split(
        "\n",
      );
      expect(lines).toEqual(want.slice(0, -1));
      const chunks = run.frames.map((f) => f.data);
      const expected = Expected.parse(
        JSON.parse(readFileSync(join(c.dir, "expected.json"), "utf8")),
      ).messages;
      if (run.protocol === "ai-sdk") {
        expect(await aiSdkProblems(chunks)).toEqual([]);
        const prior = run.received.map((f) => f.data);
        const message = await aiSdkFold([...prior, ...chunks]);
        expect(aiSdkProjection(message)).toEqual(expected);
        return;
      }
      expect(agUiProblems(chunks)).toEqual([]);
      const stock = agUiProjection(await agUiFold(chunks));
      expect(stock).toEqual(expected);
      expect(agUiProjection(threadsFold(chunks))).toEqual(stock);
    });
  });
}
