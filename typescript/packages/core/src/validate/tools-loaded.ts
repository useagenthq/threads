import type { EventOf, Fold } from "../fold/state";
import { invalid, type Violation } from "./violation";

// Rule 46: a tools_loaded directly follows its tool_search's successful result and loads only
// tools deferred in reference form, each by the spec_ref it was pinned with. Its artifact checks
// need artifacts, so import makes them (render/tool-specs.ts).

export function checkToolsLoaded(
  fold: Fold,
  e: EventOf<"tools_loaded">,
): Violation {
  const call = fold.calls.get(e.data.call_id);
  const result = call?.result;
  if (
    call?.spec?.name !== "tool_search" ||
    result?.type !== "tool_result" ||
    result.seq !== fold.seq ||
    result.data.is_error
  )
    return invalid(
      "a tools_loaded directly follows its tool_search call's successful result",
    );
  const names = new Set<string>();
  for (const { name, spec_ref: ref } of e.data.tools) {
    const current = fold.tools.find((t) => t.name === name);
    const pinned = fold.knownTools.get(name)?.spec_ref;
    if (names.has(name) || fold.loaded.has(name))
      return invalid(`tools_loaded loads ${name} twice`);
    names.add(name);
    if (current?.spec_ref === undefined || current.defer_loading !== true)
      return invalid(
        `tools_loaded loads ${name}, not deferred in reference form`,
      );
    if (
      pinned?.sha256 !== ref.sha256 ||
      pinned.bytes !== ref.bytes ||
      pinned.media_type !== ref.media_type
    )
      return invalid(
        `tools_loaded names ${name} by a spec_ref it wasn't pinned with`,
      );
  }
  return undefined;
}
