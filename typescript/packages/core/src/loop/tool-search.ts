import { type EventOf, type Fold, isDeferred } from "../fold/state";
import { ToolSearchInput } from "../tools/agent-inputs";
import { MAX_QUERY, search } from "../tools/tool-search";
import { draft, TOOL } from "./drafts";
import type { Session } from "./session";
import type { Halt } from "./types";

// tool_search (spec/schema/README.md, "Deferred tools and tool_search"): read_only and
// deterministic over the log, so a crash before its batch just runs it again. Its result and
// the tools_loaded naming what it loads are one batch.

/**
 * The framework's tool_search: pinned exactly when something is deferred, and setup refuses a
 * user tool with its name then. Otherwise the name is free for a user tool.
 */
export function searchPinned(fold: Fold): boolean {
  return [...fold.knownTools.values()].some((t) => t.defer_loading === true);
}

/** Beyond the catalog schema: the query's length, counted in code points. */
export function queryTooLong(input: unknown): string | undefined {
  const parsed = ToolSearchInput.safeParse(input);
  return parsed.success && [...parsed.data.query].length > MAX_QUERY
    ? `invalid input: query is longer than ${MAX_QUERY} characters`
    : undefined;
}

export function searchTools(
  s: Session,
  call: EventOf<"tool_call">,
): Promise<Halt | undefined> {
  const { call_id } = call.data;
  // Arguments, the query's length included, were checked before authorization.
  const { query, limit } = ToolSearchInput.parse(call.data.input);
  // Only reference-form tools load through tools_loaded; a legacy inline-deferred one is neither.
  const deferred = s.fold.tools.filter(
    (t) => isDeferred(s.fold, t) && t.spec_ref !== undefined,
  );
  const others = s.fold.tools
    .filter((t) => !isDeferred(s.fold, t))
    .map((t) => t.name);
  const found = search(query, limit, deferred, others);
  const result = draft.toolResult(
    {
      call_id,
      is_error: false,
      origin: "executed",
      preview: found.lines.join("\n"),
    },
    TOOL,
  );
  const tools = found.loaded.flatMap((name) => {
    const ref = s.fold.knownTools.get(name)?.spec_ref;
    return ref === undefined ? [] : [{ name, spec_ref: ref }];
  });
  const [first, ...rest] = tools;
  return first === undefined
    ? s.append(result)
    : s.append(result, draft.toolsLoaded({ call_id, tools: [first, ...rest] }));
}
