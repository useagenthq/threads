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
import {
  assertNever,
  JsonObject,
  loadedTools,
  readOrRefuse,
  Unsendable,
} from "@threads/core/adapter";
import { z } from "zod";
import { cacheControl, type Ttl } from "./caching";

// Render v1 → a Messages API request body, deterministically: the same request and artifacts
// always give the same body. Every value comes from the render: model, params
// and hosted tools from line 0, never from the factory.

export type Mapped =
  | {
      readonly ok: true;
      readonly body: { readonly [key: string]: Json };
      /** Document artifact sha256 by Anthropic document_index, for citations. */
      readonly documents: readonly string[];
      /** The pinned prompt_cache TTL: a cache write under any other is unknown. */
      readonly ttl: Ttl | undefined;
    }
  | {
      readonly ok: false;
      readonly code: Unsendable["code"];
      readonly message: string;
    };

type Block = { readonly [key: string]: Json };
type Message = { role: "user" | "assistant"; content: Block[] };

const utf8 = new TextDecoder("utf-8", { fatal: true });
/** Adapter setting prompt_cache, read from line 0; any other value is refused before dispatch. */
const PromptCache = z.enum(["5m", "1h"]).optional();

export async function toAnthropic(
  request: RenderRequest,
  context: ModelContext,
): Promise<Mapped> {
  if (request.head.adapter.name !== "anthropic")
    return {
      ok: false,
      code: "provider_error",
      message: `line 0 names adapter ${request.head.adapter.name}, not anthropic`,
    };
  const { head } = request;
  const { settings } = head.adapter;
  const ttl = PromptCache.safeParse(settings["prompt_cache"]);
  if (!ttl.success)
    return {
      ok: false,
      code: "continuation_unsupported",
      message: `line 0 adapter setting prompt_cache must be "5m" or "1h", not ${JSON.stringify(settings["prompt_cache"])}`,
    };
  const documents: string[] = [];
  const ctx = {
    bytes: readOrRefuse(context),
    documents,
    citations: settings["citations"] === true,
  };
  const messages: Message[] = [];
  try {
    for (const line of request.lines) {
      const message = await lineMessage(line, ctx);
      if (message !== undefined) append(messages, message);
    }
  } catch (error) {
    if (error instanceof Unsendable)
      return { ok: false, code: error.code, message: error.message };
    throw error;
  }
  const hosted = settings["hosted_tools"];
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
    ttl: ttl.data,
    body: {
      model: head.model.name,
      ...head.params,
      ...cached(head.system, tools, ttl.data),
      messages: messages.map(resultsFirst),
      stream: true,
    },
  };
}

/**
 * System and tools, with the cache controls prompt_cache asks for: automatic caching of the
 * history (top level) and one breakpoint at the end of line 0, on the system block, else on the
 * last tool. A function of the settings, system and current tool set, never of message content.
 */
function cached(
  system: string,
  tools: readonly Json[],
  ttl: Ttl | undefined,
): { readonly [key: string]: Json } {
  const withTools = tools.length === 0 ? {} : { tools: [...tools] };
  if (ttl === undefined)
    return { ...(system === "" ? {} : { system }), ...withTools };
  const cache_control = cacheControl(ttl);
  if (system !== "")
    return {
      cache_control,
      system: [{ type: "text", text: system, cache_control }],
      ...withTools,
    };
  const last = tools.at(-1);
  if (last === undefined) return { cache_control };
  return {
    cache_control,
    tools: [
      ...tools.slice(0, -1),
      { ...JsonObject.parse(last), cache_control },
    ],
  };
}

/** The API requires tool results before any other block of a user message; the rest keep order. */
function resultsFirst(message: Message): Message {
  if (message.role !== "user") return message;
  const isResult = (b: Block) => b["type"] === "tool_result";
  return {
    role: "user",
    content: [
      ...message.content.filter(isResult),
      ...message.content.filter((b) => !isResult(b)),
    ],
  };
}

type Ctx = {
  readonly bytes: (ref: ArtifactRef) => Promise<Uint8Array>;
  readonly documents: string[];
  /** Adapter setting citations: enabled on every document, as the provider requires. */
  readonly citations: boolean;
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
          "content_unsupported",
          "anthropic takes no audio input",
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
  const meta = {
    ...(part.title === undefined ? {} : { title: part.title }),
    ...(ctx.citations ? { citations: { enabled: true } } : {}),
  };
  if (part.ref.media_type === "application/pdf")
    return {
      type: "document",
      source: {
        type: "base64",
        media_type: "application/pdf",
        data: data.toBase64(),
      },
      ...meta,
    };
  return {
    type: "document",
    source: { type: "text", media_type: "text/plain", data: utf8.decode(data) },
    ...meta,
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
            "continuation_unsupported",
            `a ${part.provider} ${part.format} part can't be sent to anthropic`,
          );
        out.push(
          JsonObject.parse(JSON.parse(utf8.decode(await ctx.bytes(part.ref)))),
        );
        break;
      case "citation":
        break;
      case "image_ref":
        throw new Unsendable(
          "continuation_unsupported",
          "anthropic takes no assistant images",
        );
      default:
        assertNever(part);
    }
  }
  return out;
}
