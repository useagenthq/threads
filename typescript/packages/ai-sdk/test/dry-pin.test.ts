import { dryPinCases } from "@threads/adapter-testkit";
import { aiSdk } from "../src";
import { fakeModel } from "./fake";

// Lane 22 (test 4d): threads eval --agent pins this adapter without setup, and gets the same
// line 0 and config_hash a real run pins after setup. The AI SDK model carries its own
// credentials, so no env variable is read either way.

dryPinCases({
  factory: "aiSdk",
  env: "THREADS_AI_SDK_UNUSED_KEY",
  model: () =>
    aiSdk({
      model: fakeModel([]).factory,
      maxInputTokens: 128_000,
      maxOutputTokens: 8192,
      cacheTtlMs: 300_000,
    }),
});
