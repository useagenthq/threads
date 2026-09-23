import type { ModelInfo } from "./protocol";
import type { RenderRequest } from "./render-lines";

// a request whose parts the model can't take fails before dispatch
// with a typed code, so no attempt is recorded and nothing reaches the provider.

export type Unsupported = {
  readonly code:
    | "content_unsupported"
    | "continuation_unsupported"
    | "transport_fence_unsupported";
  readonly message: string;
};

const MEDIA = new Set(["image_ref", "document_ref", "audio_ref"]);

/** The first part of `request` that `info` doesn't declare it can take, if any. */
export function unsupported(
  request: RenderRequest,
  info: ModelInfo,
): Unsupported | undefined {
  const accepts = new Set<string>(info.accepts);
  const provider = info.model.provider;
  for (const line of request.lines) {
    if (line.role === "tools") continue;
    for (const part of line.content) {
      if (
        line.role !== "assistant" &&
        MEDIA.has(part.type) &&
        !accepts.has(part.type)
      )
        return {
          code: "content_unsupported",
          message: `${provider}/${info.model.name} does not accept ${part.type}`,
        };
      if (
        (part.type === "reasoning" || part.type === "hosted_tool") &&
        part.provider !== provider
      )
        return {
          code: "continuation_unsupported",
          message: `a ${part.provider} ${part.format} part can't be sent to ${provider}`,
        };
    }
  }
  return undefined;
}
