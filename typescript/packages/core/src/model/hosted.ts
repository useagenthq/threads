import { ConfigError } from "../agent/errors";
import type { JsonObject } from "../log";

// Provider-hosted tools: the provider executes them inside one model attempt,
// so they bypass the pre-dispatch barrier. An adapter offers only those that are read-only
// toward the outside world (web search, web fetch); they are pinned in line 0 as
// adapter.settings.hosted_tools and recorded as hosted_tool and citation parts. Everything else
// (code interpreter, file search, hosted MCP, computer use, image generation) is refused.

/** Throws hosted_tool_unsupported for a hosted tool whose type isn't on `allowed`. */
export function checkHostedTools(
  adapter: string,
  tools: readonly JsonObject[],
  allowed: RegExp,
): void {
  for (const tool of tools) {
    const type = tool["type"];
    if (typeof type !== "string" || !allowed.test(type))
      throw new ConfigError(
        "hosted_tool_unsupported",
        `${adapter}: hosted tool ${JSON.stringify(type ?? null)} is not read-only toward the outside world; only hosted web search and fetch are allowed`,
      );
  }
}
