import { type EventOf, isDeferred } from "../fold/state";
import { ToolSearchInput } from "../tools/agent-inputs";
import { MAX_QUERY, search } from "../tools/tool-search";
import { draft, TOOL } from "./drafts";
import type { Session } from "./session";
import type { Halt } from "./types";

// tool_search (spec/schema/README.md, "Deferred tools and tool_search"): read_only and
// deterministic over the log, so a crash before its batch just runs it again. Its result and
// the tools_loaded naming what it loads are one batch.

export function searchTools(
  s: Session,
  call: EventOf<"tool_call">,
): Halt | undefined {
  const { call_id } = call.data;
  // Arguments already parsed before authorization; the length is counted in code points.
  const { query, limit } = ToolSearchInput.parse(call.data.input);
  if ([...query].length > MAX_QUERY)
    return s.append(
      draft.toolResult(
        {
          call_id,
          is_error: true,
          origin: "not_executed",
          preview: `invalid input: query is longer than ${MAX_QUERY} characters`,
        },
        TOOL,
      ),
    );
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
