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
import { assertNever, JsonObject, loadedTools } from "@threads/core/adapter";

// Render v1 → a Messages API request body, deterministically: the same request and artifacts
// always give the same body. Every value comes from the render: model, params
// and hosted tools from line 0, never from the factory.

export type Mapped =
  | {
      readonly ok: true;
      readonly body: { readonly [key: string]: Json };
      /** Document artifact sha256 by Anthropic document_index, for citations. */
      readonly documents: readonly string[];
    }
  | { readonly ok: false; readonly message: string };

type Block = { readonly [key: string]: Json };
type Message = { role: "user" | "assistant"; content: Block[] };

/** Thrown inside the mapper and caught at its edge: a part this adapter can't send. */
class Unsendable extends Error {}

const utf8 = new TextDecoder("utf-8", { fatal: true });

export async function toAnthropic(
  request: RenderRequest,
  context: ModelContext,
): Promise<Mapped> {
  if (request.head.adapter.name !== "anthropic")
    return {
      ok: false,
      message: `line 0 names adapter ${request.head.adapter.name}, not anthropic`,
    };
  const documents: string[] = [];
  const bytes = async (ref: ArtifactRef): Promise<Uint8Array> => {
    const read = await context.read(ref);
    if (!read.ok) throw new Unsendable(read.error.message);
    return read.value;
  };
  const messages: Message[] = [];
  try {
    for (const line of request.lines) {
      const message = await lineMessage(line, { bytes, documents });
      if (message !== undefined) append(messages, message);
    }
  } catch (error) {
    if (error instanceof Unsendable)
      return { ok: false, message: error.message };
    throw error;
  }
  const { head } = request;
  const hosted = head.adapter.settings["hosted_tools"];
  const tools: Json[] = [
    ...loadedTools(request).map((t) => ({
      name: t.name,
      description: t.description,
      input_schema: t.input_schema,
    })),
    ...(Array.isArray(hosted) ? hosted : []),
  ];
  return {
    ok: true,
    documents,
    body: {
      model: head.model.name,
      ...head.params,
      ...(head.system === "" ? {} : { system: head.system }),
      ...(tools.length === 0 ? {} : { tools }),
      messages,
      stream: true,
    },
  };
}

type Ctx = {
  readonly bytes: (ref: ArtifactRef) => Promise<Uint8Array>;
  readonly documents: string[];
};

/** Consecutive messages of one role merge, so the roles alternate as the API expects. */
function append(messages: Message[], next: Message): void {
  const last = messages.at(-1);
  if (last?.role === next.role) last.content.push(...next.content);
  else messages.push(next);
}

async function lineMessage(
  line: RenderLine,
  ctx: Ctx,
): Promise<Message | undefined> {
  switch (line.role) {
    case "user":
      return { role: "user", content: await blocks(line.content, ctx) };
    case "assistant":
      return { role: "assistant", content: await assistant(line.content, ctx) };
    case "tool": {
      const content = await blocks(line.content, ctx);
      if (line.late === true)
        return {
          role: "user",
          content: [
            {
              type: "text",
              text: `[late tool result: call_id=${line.call_id}]`,
            },
            ...content,
          ],
        };
      return {
        role: "user",
        content: [
          {
            type: "tool_result",
            tool_use_id: line.call_id,
            is_error: line.is_error,
            content,
          },
        ],
      };
    }
    case "tools":
      return undefined;
    default:
      return assertNever(line);
  }
}

/** User and tool result parts. A citation annotates text; the API takes none back. */
async function blocks(
  parts: readonly (InputPart | ResultPart)[],
  ctx: Ctx,
): Promise<Block[]> {
  const out: Block[] = [];
  for (const part of parts) {
    switch (part.type) {
      case "text":
        out.push({ type: "text", text: part.text });
        break;
      case "image_ref":
        out.push({
          type: "image",
          source: {
            type: "base64",
            media_type: part.ref.media_type,
            data: (await ctx.bytes(part.ref)).toBase64(),
          },
        });
        break;
      case "document_ref":
        out.push(await document(part, ctx));
        break;
      case "audio_ref":
        throw new Unsendable(
          "content_unsupported: anthropic takes no audio input",
        );
      case "citation":
        break;
      default:
        assertNever(part);
    }
  }
  return out;
}

async function document(
  part: Extract<InputPart, { type: "document_ref" }>,
  ctx: Ctx,
): Promise<Block> {
  const data = await ctx.bytes(part.ref);
  ctx.documents.push(part.ref.sha256);
  const title = part.title === undefined ? {} : { title: part.title };
  if (part.ref.media_type === "application/pdf")
    return {
      type: "document",
      source: {
        type: "base64",
        media_type: "application/pdf",
        data: data.toBase64(),
      },
      ...title,
    };
  return {
    type: "document",
    source: { type: "text", media_type: "text/plain", data: utf8.decode(data) },
    ...title,
  };
}

/** Assistant parts; reasoning and hosted tool blocks go back exactly as recorded. */
async function assistant(
  parts: readonly OutputPart[],
  ctx: Ctx,
): Promise<Block[]> {
  const out: Block[] = [];
  for (const part of parts) {
    switch (part.type) {
      case "text":
        out.push({ type: "text", text: part.text });
        break;
      case "tool_use":
        out.push({
          type: "tool_use",
          id: part.call_id,
          name: part.name,
          input: part.input,
        });
        break;
      case "reasoning":
      case "hosted_tool":
        if (part.provider !== "anthropic")
          throw new Unsendable(
            `continuation_unsupported: a ${part.provider} ${part.format} part can't be sent to anthropic`,
          );
        out.push(
          JsonObject.parse(JSON.parse(utf8.decode(await ctx.bytes(part.ref)))),
        );
        break;
      case "citation":
        break;
      case "image_ref":
        throw new Unsendable(
          "continuation_unsupported: anthropic takes no assistant images",
        );
      default:
        assertNever(part);
    }
  }
  return out;
}
