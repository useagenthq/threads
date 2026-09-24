import { describe, expect, test } from "bun:test";
import { spans } from "../src/spans";
import { goldens } from "./goldens";

// threads.cut is defensive: no path on main ends a turn with a call or model request still
// open, so it is proved on a synthetic chain (validation bypassed) of the pure derivation.

const golden = goldens().find((g) => g.name === "otel-turn-model-tool");

describe("spans()", () => {
  test("a turn that ends with a call and a request still open closes them as cut", () => {
    const branchId = golden?.branches[0] ?? "";
    const chain = golden?.chains.get(branchId) ?? [];
    // The tool result (seq 7) and the second request's response (seq 9) are never written.
    const cutShort = chain.filter(
      (l) => l.event.seq !== 7 && l.event.seq !== 9,
    );
    const all = spans(
      { tenant: "local", branchId, chain: cutShort, content: false },
      () => undefined,
    );
    const cut = all.filter((s) => s.attributes["threads.cut"] === true);
    expect(cut.map((s) => s.name).toSorted()).toEqual([
      "chat scripted-1",
      "execute_tool read_file",
    ]);
    for (const s of cut) {
      expect(s.status).toBe("cut");
      expect(s.closeSeq).toBe(10);
    }
    const turn = all.find((s) => s.name === "invoke_agent demo");
    expect(turn?.status).toBeUndefined();
    expect(turn?.attributes["threads.turn.reason"]).toBe("end_turn");
  });

  test("a turn still open exports nothing yet; its closed children go as they close", () => {
    const branchId = golden?.branches[0] ?? "";
    const chain = (golden?.chains.get(branchId) ?? []).filter(
      (l) => l.event.seq <= 7,
    );
    const all = spans(
      { tenant: "local", branchId, chain, content: false },
      () => undefined,
    );
    expect(all.map((s) => s.name)).toEqual([
      "chat scripted-1",
      "execute_tool read_file",
    ]);
  });
});
