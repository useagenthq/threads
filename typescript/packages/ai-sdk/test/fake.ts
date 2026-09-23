import type {
  LanguageModelV4,
  LanguageModelV4CallOptions,
  LanguageModelV4StreamPart,
} from "@ai-sdk/provider";

// A hand-written AI SDK v4 model: each doStream call takes the next script entry, which either
// throws (a rejection before the stream) or streams its parts. Calls are recorded.

export type Entry =
  | readonly LanguageModelV4StreamPart[]
  | { readonly throws: unknown }
  | ((
      options: LanguageModelV4CallOptions,
    ) => Promise<readonly LanguageModelV4StreamPart[]>);

export function fakeModel(script: readonly Entry[]): {
  readonly model: LanguageModelV4;
  readonly calls: LanguageModelV4CallOptions[];
} {
  const queue = [...script];
  const calls: LanguageModelV4CallOptions[] = [];
  const model: LanguageModelV4 = {
    specificationVersion: "v4",
    provider: "acme.chat",
    modelId: "acme-1",
    supportedUrls: {},
    doGenerate: async () => {
      throw new Error("the bridge only streams");
    },
    doStream: async (options) => {
      calls.push(options);
      const entry = queue.shift();
      if (entry === undefined) throw new Error("fakeModel: script ended");
      if ("throws" in entry) throw entry.throws;
      const parts = typeof entry === "function" ? await entry(options) : entry;
      return {
        stream: new ReadableStream<LanguageModelV4StreamPart>({
          start(controller) {
            for (const part of parts) controller.enqueue(part);
            controller.close();
          },
        }),
      };
    },
  };
  return { model, calls };
}

export const usage = {
  inputTokens: { total: 120, noCache: 20, cacheRead: 100, cacheWrite: 0 },
  outputTokens: { total: 30, text: 18, reasoning: 12 },
};

export const unknownUsage = {
  inputTokens: {
    total: undefined,
    noCache: undefined,
    cacheRead: undefined,
    cacheWrite: undefined,
  },
  outputTokens: { total: undefined, text: undefined, reasoning: undefined },
};
