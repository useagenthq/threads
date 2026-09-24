import { z } from "zod";
import {
  AdapterRef,
  InputPart,
  JsonObject,
  ModelRef,
  Name,
  OutputPart,
  ResultPart,
} from "../log";
import type { Arr, Lit, Opt, Strict } from "../log/zod-types";

// The Render v1 lines an adapter maps to a provider request (render/lines.ts and
// render/prefix.ts write them). Parsed, so an adapter works with typed parts.

type LoadedTool = Strict<{
  name: typeof Name;
  description: z.ZodString;
  input_schema: typeof JsonObject;
}>;
type DeferredTool = Strict<{
  name: typeof Name;
  description: z.ZodString;
  deferred: Lit<true>;
}>;
const LoadedTool: LoadedTool = z.strictObject({
  name: Name,
  description: z.string(),
  input_schema: JsonObject,
});
const DeferredTool: DeferredTool = z.strictObject({
  name: Name,
  description: z.string(),
  deferred: z.literal(true),
});
const ToolLine: z.ZodUnion<[LoadedTool, DeferredTool]> = z.union([
  LoadedTool,
  DeferredTool,
]);
export type ToolLine = z.infer<typeof ToolLine>;

const Line0: Strict<{
  adapter: typeof AdapterRef;
  model: typeof ModelRef;
  params: typeof JsonObject;
  system: z.ZodString;
  tools: Arr<typeof ToolLine>;
}> = z.strictObject({
  adapter: AdapterRef,
  model: ModelRef,
  params: JsonObject,
  system: z.string(),
  tools: z.array(ToolLine),
});
export type Line0 = z.infer<typeof Line0>;

type UserLine = Strict<{ role: Lit<"user">; content: Arr<typeof InputPart> }>;
type AssistantLine = Strict<{
  role: Lit<"assistant">;
  content: Arr<typeof OutputPart>;
}>;
type ToolResultLine = Strict<{
  role: Lit<"tool">;
  call_id: z.ZodString;
  is_error: z.ZodBoolean;
  content: Arr<typeof ResultPart>;
  late: Opt<Lit<true>>;
}>;
type ToolsLine = Strict<{ role: Lit<"tools">; tools: Arr<typeof ToolLine> }>;
type ToolsLoadedLine = Strict<{
  role: Lit<"tools_loaded">;
  tools: Arr<LoadedTool>;
}>;

const Line: z.ZodDiscriminatedUnion<
  [UserLine, AssistantLine, ToolResultLine, ToolsLine, ToolsLoadedLine],
  "role"
> = z.discriminatedUnion("role", [
  z.strictObject({ role: z.literal("user"), content: z.array(InputPart) }),
  z.strictObject({
    role: z.literal("assistant"),
    content: z.array(OutputPart),
  }),
  z.strictObject({
    role: z.literal("tool"),
    call_id: z.string(),
    is_error: z.boolean(),
    content: z.array(ResultPart),
    late: z.literal(true).optional(),
  }),
  z.strictObject({ role: z.literal("tools"), tools: z.array(ToolLine) }),
  z.strictObject({
    role: z.literal("tools_loaded"),
    tools: z.array(LoadedTool),
  }),
]);
export type RenderLine = z.infer<typeof Line>;

/** A parsed Render v1 request: line 0 and the history lines after it, in order. */
export type RenderRequest = {
  readonly head: Line0;
  readonly lines: readonly RenderLine[];
};

const decoder = new TextDecoder("utf-8", { fatal: true });

/**
 * Parses Render v1 bytes. The loop rendered them, so a line that doesn't parse is a bug and
 * throws.
 */
export function parseRender(body: Uint8Array): RenderRequest {
  const [first, ...rest] = decoder.decode(body).split("\n").slice(0, -1);
  if (first === undefined) throw new Error("Render v1 request has no line 0");
  return {
    head: Line0.parse(JSON.parse(first)),
    lines: rest.map((line) => Line.parse(JSON.parse(line))),
  };
}

/**
 * The tools a request offers (Render v1, "Provider tools"): the latest complete set (line 0's,
 * replaced by each later `tools` line) without its stubs, which have no schema, then every spec
 * of the `tools_loaded` lines after it.
 */
export function loadedTools(
  request: RenderRequest,
): readonly z.infer<typeof LoadedTool>[] {
  let tools: z.infer<typeof LoadedTool>[] = request.head.tools.flatMap(full);
  for (const line of request.lines)
    if (line.role === "tools") tools = line.tools.flatMap(full);
    else if (line.role === "tools_loaded") tools = [...tools, ...line.tools];
  return tools;
}

const full = (t: ToolLine): z.infer<typeof LoadedTool>[] =>
  "input_schema" in t ? [t] : [];
