import { describe, expect, test } from "bun:test";
import { openai } from "../src";

// (F1.16, F1.17): only hosted web search is allowed, pinned in line 0 as
// adapter.settings.hosted_tools; code interpreter, file search, hosted MCP, computer use and
// image generation fail at construction.

const options = {
  apiKey: "test-key",
};

describe("openai hosted tools", () => {
  test("web search is pinned in line 0", () => {
    const hostedTools = [
      { type: "web_search" },
      { type: "web_search_preview_2025_03_11" },
    ];
    const { info } = openai("gpt-5.5", { ...options, hostedTools });
    expect(info.adapter.settings).toEqual({ hosted_tools: hostedTools });
    expect(info.hosted_tools).toEqual([
      "web_search",
      "web_search_preview_2025_03_11",
    ]);
  });

  test.each([
    "code_interpreter",
    "file_search",
    "mcp",
    "computer_use_preview",
    "image_generation",
    "local_shell",
  ])("%s is refused with hosted_tool_unsupported", (type) => {
    expect(() =>
      openai("gpt-5.5", { ...options, hostedTools: [{ type }] }),
    ).toThrow(expect.objectContaining({ code: "hosted_tool_unsupported" }));
  });
});
