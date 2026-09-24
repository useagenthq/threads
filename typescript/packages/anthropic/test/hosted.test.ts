import { describe, expect, test } from "bun:test";
import { anthropic } from "../src";

// (F1.16, F1.17): only server tools read-only toward the outside world are
// allowed, pinned in line 0 as adapter.settings.hosted_tools; the rest fail at construction.

const options = {
  maxTokens: 1024,
  apiKey: "test-key",
};

describe("anthropic hosted tools", () => {
  test("web search and web fetch are pinned in line 0", () => {
    const hostedTools = [
      { type: "web_search_20250305", name: "web_search", max_uses: 3 },
      { type: "web_fetch_20250910", name: "web_fetch" },
    ];
    const { info } = anthropic("claude-sonnet-5", { ...options, hostedTools });
    expect(info.adapter.settings).toEqual({ hosted_tools: hostedTools });
    expect(info.hosted_tools).toEqual(["web_search", "web_fetch"]);
  });

  test.each([
    "code_execution_20250825",
    "computer_20250124",
    "bash_20250124",
    "text_editor_20250728",
    "mcp_toolset",
    "web_search",
  ])("%s is refused with hosted_tool_unsupported", (type) => {
    expect(() =>
      anthropic("claude-sonnet-5", {
        ...options,
        hostedTools: [{ type, name: "x" }],
      }),
    ).toThrow(expect.objectContaining({ code: "hosted_tool_unsupported" }));
  });
});
