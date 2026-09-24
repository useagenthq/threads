import { dryPinCases } from "@threads/adapter-testkit";
import { openai } from "../src";

// Lane 22 (test 4d): threads eval --agent pins this adapter without setup or a key, and gets
// the same line 0 and config_hash a real run pins after setup.

const offline = async (): Promise<Response> => {
  throw new Error("no network in dry-pin tests");
};

dryPinCases({
  factory: "openai",
  env: "OPENAI_API_KEY",
  model: () => openai("gpt-5.5", { fetch: offline }),
});
