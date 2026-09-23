import { describe, expect, test } from "bun:test";
import {
  drain,
  recordingFetch,
  renderBody,
  sse,
} from "@threads/adapter-testkit";
import { ConfigError, type Fetch, memoryContext } from "@threads/core/adapter";
import { aiSdk } from "../src";
import { fakeModel, offline } from "./fake";

// The lease check at the provider's real send point: a send can carry
// provider-hosted tools, so no provider request may leave without it.

const limits = { contextWindow: 128_000, maxOutputTokens: 8192 };
const hi = { role: "user", content: [{ type: "text", text: "hi" }] };
const finish = (unified: "stop") => ({
  type: "finish" as const,
  finishReason: { unified, raw: undefined },
  usage: {
    inputTokens: { total: 1, noCache: 1, cacheRead: 0, cacheWrite: 0 },
    outputTokens: { total: 1, text: 1, reasoning: 0 },
  },
});

describe("fencing", () => {
  test("a stale writer never reaches doStream", async () => {
    const { model, calls } = fakeModel([[finish("stop")]]);
    const m = aiSdk({ model: () => model, ...limits });
    const head = {
      adapter: m.info.adapter,
      model: m.info.model,
      params: m.info.params,
      system: "",
      tools: [],
    };
    const { chunks } = await drain(
      m.send(
        { request_id: "b:e1", body: renderBody([head, hi]) },
        memoryContext(() => false),
      ),
    );
    expect(chunks).toEqual([{ kind: "rejected", reason: "stale_epoch" }]);
    expect(calls).toHaveLength(0);
  });

  test("the provider's fetch re-checks the lease at its real send point", async () => {
    let lost = false;
    const transport = recordingFetch([sse([])]);
    let providerFetch: Fetch | undefined;
    const { model, calls } = fakeModel([
      async () => {
        lost = true; // the lease is lost while the provider prepares its request
        await providerFetch?.("https://acme.dev/chat", {
          method: "POST",
          body: "{}",
        });
        return [finish("stop")];
      },
    ]);
    const m = aiSdk({
      model: (fetch) => {
        providerFetch = fetch;
        return model;
      },
      fetch: transport.fetch,
      ...limits,
    });
    const head = {
      adapter: m.info.adapter,
      model: m.info.model,
      params: m.info.params,
      system: "",
      tools: [],
    };
    const { chunks, thrown } = await drain(
      m.send(
        { request_id: "b:e1", body: renderBody([head, hi]) },
        memoryContext(() => !lost),
      ),
    );
    expect(calls).toHaveLength(1);
    expect(transport.calls).toHaveLength(0);
    expect(thrown).toBeUndefined();
    expect(chunks).toEqual([{ kind: "rejected", reason: "stale_epoch" }]);
  });

  test("a pre-built model is refused: its transport can't be fenced", () => {
    const { model } = fakeModel([]);
    const prebuilt: unknown = model;
    expect(() =>
      // @ts-expect-error: a JavaScript caller passing a model instead of a factory
      aiSdk({ model: prebuilt, ...limits }),
    ).toThrow(ConfigError);
    try {
      // @ts-expect-error: as above, to read the code
      aiSdk({ model: prebuilt, ...limits });
    } catch (error) {
      expect(error instanceof ConfigError ? error.code : undefined).toBe(
        "transport_fence_unsupported",
      );
    }
  });

  test("the provider's fetch sends nothing outside a threads send", async () => {
    const transport = recordingFetch([sse([])]);
    let providerFetch: Fetch | undefined;
    const { model } = fakeModel([]);
    aiSdk({
      model: (fetch) => {
        providerFetch = fetch;
        return model;
      },
      fetch: transport.fetch,
      ...limits,
    });
    await expect(providerFetch?.("https://acme.dev/chat")).rejects.toThrow();
    expect(transport.calls).toHaveLength(0);
  });

  test("a model that bypasses the fenced fetch is caught and then refused", async () => {
    const { model } = fakeModel([[finish("stop")], [finish("stop")]]);
    // The factory ignores threads' fetch: the provider would send on its own transport.
    const m = aiSdk({ model: () => model, fetch: offline, ...limits });
    const head = {
      adapter: m.info.adapter,
      model: m.info.model,
      params: m.info.params,
      system: "",
      tools: [],
    };
    const send = () =>
      drain(
        m.send(
          { request_id: "b:e1", body: renderBody([head, hi]) },
          memoryContext(() => true),
        ),
      );
    // The request left unfenced, so its outcome is unknown: a broken stream, no output kept.
    const first = await send();
    expect(first.chunks).toEqual([]);
    expect(first.thrown).toBeInstanceOf(ConfigError);
    // From then on nothing leaves: refused before doStream.
    const second = await send();
    expect(second.thrown).toBeUndefined();
    expect(second.chunks).toEqual([
      { kind: "rejected", reason: "provider_error" },
    ]);
  });
});
