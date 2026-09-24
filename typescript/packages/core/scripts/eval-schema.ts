import { z } from "zod";
import {
  CaseLine0,
  CaseOffline,
  CaseSnapshot,
  ExtensionScript,
  SandboxResults,
} from "../src/evals/files";
import {
  ChildThreadSource,
  FrameworkEvent,
  FrameworkSource,
  ObservationHook,
  RecordedHook,
  ScriptableEvent,
  ScriptableSource,
} from "../src/evals/kinds";
import { EvalReport, JudgeInput, Rubric, Verdicts } from "../src/evals/schema";
import { type Node, tidy } from "./spec-schema";

// Builds spec/schema/eval.v1.schema.json from the Zod eval shapes. A def the event schema
// already has is referenced there by URN, never copied, so each shape has one definition.

const EVENTS = "urn:threads:schema:events:v1";

function isNode(value: unknown): value is Node {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Every `#/$defs/<name>` of an event-schema def becomes the event schema's URN ref. */
function external(value: Node, shared: ReadonlySet<string>): Node {
  const walk = (v: Node[string]): Node[string] => {
    if (Array.isArray(v)) return v.map(walk);
    return isNode(v) ? external(v, shared) : v;
  };
  return Object.fromEntries(
    Object.entries(value).map(([key, v]) => {
      const name = typeof v === "string" ? v.replace("#/$defs/", "") : "";
      return key === "$ref" && shared.has(name)
        ? [key, `${EVENTS}#/$defs/${name}`]
        : [key, walk(v)];
    }),
  );
}

export function evalSchema(events: Node): Node {
  const exported = z.toJSONSchema(
    z.tuple([
      EvalReport,
      Verdicts,
      JudgeInput,
      Rubric,
      SandboxResults,
      ExtensionScript,
      CaseOffline,
      CaseSnapshot,
      CaseLine0,
      RecordedHook,
      ObservationHook,
      ScriptableEvent,
      FrameworkEvent,
      ScriptableSource,
      FrameworkSource,
      ChildThreadSource,
    ]),
    { target: "draft-2020-12", io: "input", unrepresentable: "throw" },
  );
  const tree: unknown = JSON.parse(JSON.stringify(exported));
  const defs = isNode(tree) ? tree["$defs"] : undefined;
  const eventDefs = events["$defs"];
  if (!isNode(defs) || !isNode(eventDefs))
    throw new Error("export has no $defs");
  // Rules name event defs by local ref too (TextOrRef), so every event def name is shared.
  const shared = new Set(Object.keys(eventDefs));
  const own = external(
    Object.fromEntries(Object.entries(defs).filter(([k]) => !shared.has(k))),
    shared,
  );
  return tidy(
    {
      $schema: "https://json-schema.org/draft/2020-12/schema",
      $id: "urn:threads:schema:eval:v1",
      title: "threads eval runner shapes",
      description:
        "The judge's verdicts and input, the eval report, the saved-case files the runner reads (sandbox.json v2, extensions.json and the case.json fields they add), and the closed hook-kind and user-event lists.",
      $defs: own,
    },
    own,
  );
}
