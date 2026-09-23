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

// Render v1 → a Responses API request body, deterministically. Every value
// comes from the render: model, params and hosted tools from line 0. Stateless: store is false
// and reasoning goes back as the recorded items, encrypted content included.

export type Mapped =
  | { readonly ok: true; readonly body: { readonly [key: string]: Json } }
  | { readonly ok: false; readonly message: string };

type Item = { readonly [key: string]: Json };

/** Thrown inside the mapper and caught at its edge: a part this adapter can't send. */
class Unsendable extends Error {}

const utf8 = new TextDecoder("utf-8", { fatal: true });
type Bytes = (ref: ArtifactRef) => Promise<Uint8Array>;

export async function toOpenAI(
  request: RenderRequest,
  context: ModelContext,
): Promise<Mapped> {
  const { head } = request;
  if (head.adapter.name !== "openai")
    return {
      ok: false,
      message: `line 0 names adapter ${head.adapter.name}, not openai`,
    };
  const bytes: Bytes = async (ref) => {
    const read = await context.read(ref);
    if (!read.ok) throw new Unsendable(read.error.message);
    return read.value;
  };
  const input: Item[] = [];
  try {
    for (const line of request.lines) input.push(...(await items(line, bytes)));
  } catch (error) {
    if (error instanceof Unsendable)
      return { ok: false, message: error.message };
    throw error;
  }
  const hosted = head.adapter.settings["hosted_tools"];
  const tools: Json[] = [
    ...loadedTools(request).map((t) => ({
      type: "function",
      name: t.name,
      description: t.description,
      parameters: t.input_schema,
      strict: false,
    })),
    ...(Array.isArray(hosted) ? hosted : []),
  ];
  return {
    ok: true,
    body: {
      model: head.model.name,
      ...head.params,
      ...(head.system === "" ? {} : { instructions: head.system }),
      ...(tools.length === 0 ? {} : { tools }),
      input,
      store: false,
      // Reasoning models only: the encrypted items are what goes back on the next request.
      ...("reasoning" in head.params
        ? { include: ["reasoning.encrypted_content"] }
        : {}),
      stream: true,
    },
  };
}

async function items(line: RenderLine, bytes: Bytes): Promise<Item[]> {
  switch (line.role) {
    case "user":
      return [{ role: "user", content: await contents(line.content, bytes) }];
    case "assistant":
      return assistant(line.content, bytes);
    case "tool": {
      const output = await contents(line.content, bytes);
      // The Responses API has no error flag on a function output: the text says it.
      const flagged: Item[] = line.is_error
        ? [{ type: "input_text", text: "[tool error]" }, ...output]
        : output;
      if (line.late === true)
        return [
          {
            role: "user",
            content: [
              {
                type: "input_text",
                text: `[late tool result: call_id=${line.call_id}]`,
              },
              ...flagged,
            ],
          },
        ];
      return [
        {
          type: "function_call_output",
          call_id: line.call_id,
          output: flagged,
        },
      ];
    }
    case "tools":
      return [];
    default:
      return assertNever(line);
  }
}

/** User and function output content. A citation annotates text; the API takes none back. */
async function contents(
  parts: readonly (InputPart | ResultPart)[],
  bytes: Bytes,
): Promise<Item[]> {
  const out: Item[] = [];
  for (const part of parts) {
    switch (part.type) {
      case "text":
        out.push({ type: "input_text", text: part.text });
        break;
      case "image_ref":
        out.push({
          type: "input_image",
          image_url: dataUrl(part.ref.media_type, await bytes(part.ref)),
          detail: "auto",
        });
        break;
      case "document_ref":
        out.push(await document(part, bytes));
        break;
      case "audio_ref":
        throw new Unsendable(
          "content_unsupported: the openai adapter takes no audio input",
        );
      case "citation":
        break;
      default:
        assertNever(part);
    }
  }
  return out;
}

function dataUrl(mediaType: string, data: Uint8Array): string {
  return `data:${mediaType};base64,${data.toBase64()}`;
}

async function document(
  part: Extract<InputPart, { type: "document_ref" }>,
  bytes: Bytes,
): Promise<Item> {
  const data = await bytes(part.ref);
  const title = part.title ?? part.ref.sha256;
  if (part.ref.media_type === "application/pdf")
    return {
      type: "input_file",
      filename: `${title}.pdf`,
      file_data: dataUrl("application/pdf", data),
    };
  return {
    type: "input_text",
    text: `<document title="${title}">\n${utf8.decode(data)}\n</document>`,
  };
}

/** Assistant parts as input items; reasoning and hosted tool items go back as recorded. */
async function assistant(
  parts: readonly OutputPart[],
  bytes: Bytes,
): Promise<Item[]> {
  const out: Item[] = [];
  for (const part of parts) {
    switch (part.type) {
      case "text":
        out.push({ role: "assistant", content: part.text });
        break;
      case "tool_use":
        out.push({
          type: "function_call",
          call_id: part.call_id,
          name: part.name,
          arguments: JSON.stringify(part.input),
        });
        break;
      case "reasoning":
      case "hosted_tool":
        if (part.provider !== "openai")
          throw new Unsendable(
            `continuation_unsupported: a ${part.provider} ${part.format} part can't be sent to openai`,
          );
        out.push(
          JsonObject.parse(JSON.parse(utf8.decode(await bytes(part.ref)))),
        );
        break;
      case "citation":
        break;
      case "image_ref":
        throw new Unsendable(
          "continuation_unsupported: the openai adapter sends no assistant images back",
        );
      default:
        assertNever(part);
    }
  }
  return out;
}
