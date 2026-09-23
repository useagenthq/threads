import type {
  JSONSchema7,
  LanguageModelV4FunctionTool,
  LanguageModelV4Message,
  LanguageModelV4Prompt,
  LanguageModelV4ToolResultOutput,
} from "@ai-sdk/provider";
import type {
  ArtifactRef,
  InputPart,
  Json,
  ModelContext,
  OutputPart,
  RenderLine,
  RenderRequest,
  ResultPart,
} from "@threads/core/adapter";
import { assertNever, loadedTools } from "@threads/core/adapter";
import { ReplayPart } from "./replay";

// Render v1 → an AI SDK language model prompt and tool list, deterministically (item
// 9). Parts go to the provider in recorded order; reasoning and hosted tool parts go back as
// the exact AI SDK parts recorded for them.

type Content<Role> = Extract<LanguageModelV4Message, { role: Role }>["content"];
type UserContent = Content<"user">;
type AssistantContent = Content<"assistant">;
type Media = InputPart["type"];

export type Mapped =
  | {
      readonly ok: true;
      readonly prompt: LanguageModelV4Prompt;
      readonly tools: LanguageModelV4FunctionTool[];
    }
  | { readonly ok: false; readonly message: string };

/** Thrown inside the mapper and caught at its edge: a part this adapter can't send. */
class Unsendable extends Error {}

const utf8 = new TextDecoder("utf-8", { fatal: true });

type Ctx = {
  readonly provider: string;
  readonly accepts: ReadonlySet<Media>;
  readonly bytes: (ref: ArtifactRef) => Promise<Uint8Array>;
  /** Tool names by call id: a tool result must name its tool. */
  readonly names: Map<string, string>;
};

export async function toPrompt(
  request: RenderRequest,
  context: ModelContext,
  accepts: readonly Media[],
): Promise<Mapped> {
  if (request.head.adapter.name !== "ai_sdk")
    return {
      ok: false,
      message: `line 0 names adapter ${request.head.adapter.name}, not ai_sdk`,
    };
  const ctx: Ctx = {
    provider: request.head.model.provider,
    accepts: new Set(accepts),
    names: new Map(),
    bytes: async (ref) => {
      const read = await context.read(ref);
      if (!read.ok) throw new Unsendable(read.error.message);
      return read.value;
    },
  };
  const prompt: LanguageModelV4Message[] =
    request.head.system === ""
      ? []
      : [{ role: "system", content: request.head.system }];
  try {
    for (const line of request.lines) {
      const message = await lineMessage(line, ctx);
      if (message !== undefined) prompt.push(message);
    }
  } catch (error) {
    if (error instanceof Unsendable)
      return { ok: false, message: error.message };
    throw error;
  }
  const tools = loadedTools(request).map(
    (t): LanguageModelV4FunctionTool => ({
      type: "function",
      name: t.name,
      description: t.description,
      inputSchema: schema(t.input_schema),
    }),
  );
  return { ok: true, prompt, tools };
}

/** A pinned tool's input_schema is a JSON Schema object (validated when the tool was pinned). */
function schema(value: { readonly [key: string]: Json }): JSONSchema7 {
  return { ...value };
}

async function lineMessage(
  line: RenderLine,
  ctx: Ctx,
): Promise<LanguageModelV4Message | undefined> {
  switch (line.role) {
    case "user":
      return { role: "user", content: await user(line.content, ctx) };
    case "assistant":
      return { role: "assistant", content: await assistant(line.content, ctx) };
    case "tool":
      return toolResult(line, ctx);
    case "tools":
      return undefined;
    default:
      return assertNever(line);
  }
}

async function toolResult(
  line: Extract<RenderLine, { role: "tool" }>,
  ctx: Ctx,
): Promise<LanguageModelV4Message> {
  const content = await user(line.content, ctx);
  if (line.late === true)
    return {
      role: "user",
      content: [
        { type: "text", text: `[late tool result: call_id=${line.call_id}]` },
        ...content,
      ],
    };
  const toolName = ctx.names.get(line.call_id);
  if (toolName === undefined)
    throw new Error(`tool result ${line.call_id} has no recorded call`);
  return {
    role: "tool",
    content: [
      {
        type: "tool-result",
        toolCallId: line.call_id,
        toolName,
        output: output(content, line.is_error),
      },
    ],
  };
}

function output(
  content: UserContent,
  isError: boolean,
): LanguageModelV4ToolResultOutput {
  const texts = content.flatMap((p) => (p.type === "text" ? [p.text] : []));
  if (texts.length === content.length) {
    const value = texts.join("");
    return isError ? { type: "error-text", value } : { type: "text", value };
  }
  // Media results have no error variant: the text says it.
  const flag = isError ? [{ type: "text" as const, text: "[tool error]" }] : [];
  return { type: "content", value: [...flag, ...content] };
}

/** User and tool result parts. A citation annotates text; providers take none back. */
async function user(
  parts: readonly (InputPart | ResultPart)[],
  ctx: Ctx,
): Promise<UserContent> {
  const out: UserContent = [];
  for (const part of parts) {
    if (part.type === "citation") continue;
    if (part.type === "text") {
      out.push({ type: "text", text: part.text });
      continue;
    }
    if (!ctx.accepts.has(part.type))
      throw new Unsendable(
        `content_unsupported: this model does not declare ${part.type} input`,
      );
    out.push({
      type: "file",
      mediaType: part.ref.media_type,
      data: { type: "data", data: await ctx.bytes(part.ref) },
      ...(part.type === "document_ref" && part.title !== undefined
        ? { filename: part.title }
        : {}),
    });
  }
  return out;
}

async function assistant(
  parts: readonly OutputPart[],
  ctx: Ctx,
): Promise<AssistantContent> {
  const out: AssistantContent = [];
  for (const part of parts) {
    switch (part.type) {
      case "text":
        out.push({ type: "text", text: part.text });
        break;
      case "tool_use":
        ctx.names.set(part.call_id, part.name);
        out.push({
          type: "tool-call",
          toolCallId: part.call_id,
          toolName: part.name,
          input: part.input,
        });
        break;
      case "reasoning":
      case "hosted_tool":
        if (part.provider !== ctx.provider)
          throw new Unsendable(
            `continuation_unsupported: a ${part.provider} ${part.format} part can't be sent to ${ctx.provider}`,
          );
        out.push(
          ReplayPart.parse(JSON.parse(utf8.decode(await ctx.bytes(part.ref)))),
        );
        break;
      case "citation":
        break;
      case "image_ref":
        throw new Unsendable(
          "continuation_unsupported: the ai-sdk bridge sends no assistant images back",
        );
      default:
        assertNever(part);
    }
  }
  return out;
}
