import type { EventOf, Fold } from "../fold/state";
import { canonicalize, JsonValue, type ToolSpec } from "../log";
import { invalid, type Violation } from "./violation";

// Rule 17, points 1-5: a new tool set never makes dispatch less safe (invariant 3).

/** Checks every spec of a tools_changed against the one pinned or first added under its name. */
export function checkToolSet(
  fold: Fold,
  e: EventOf<"tools_changed">,
): Violation {
  const { cause, tools } = e.data;
  if (cause !== undefined && cause.kind !== "tool_search")
    return invalid(`tools_changed cause ${cause.kind} has no writer`);
  if (
    cause !== undefined &&
    (cause.call_id === undefined || !fold.calls.has(cause.call_id))
  )
    return invalid("a tool_search cause names no earlier tool_call");
  const added = new Map<string, ToolSpec>();
  for (const spec of tools) {
    const first = fold.knownTools.get(spec.name) ?? added.get(spec.name);
    const violation =
      first === undefined
        ? checkAdded(spec)
        : checkKept(fold, first, spec, cause !== undefined);
    if (violation !== undefined) return violation;
    if (first === undefined) added.set(spec.name, spec);
  }
  return undefined;
}

/** Point 3: a name nothing pinned enters as unguarded, so uncertainty about it always parks. */
function checkAdded(spec: ToolSpec): Violation {
  return spec.effect_class === "unguarded" &&
    spec.dedup_window_ms === undefined &&
    spec.ends_turn === undefined
    ? undefined
    : invalid(
        `tools_changed adds ${spec.name}, which is not unguarded with no dedup window or ends_turn`,
      );
}

/** Points 1-3: the first spec holds but for defer_loading, which only goes true to absent. */
function checkKept(
  fold: Fold,
  first: ToolSpec,
  spec: ToolSpec,
  searched: boolean,
): Violation {
  if (!sameSpec(first, spec))
    return invalid(`tools_changed changes the spec of ${spec.name}`);
  const before = fold.tools.find((t) => t.name === spec.name) ?? first;
  const was = before.defer_loading === true;
  const is = spec.defer_loading === true;
  if (was === is) return undefined;
  if (is) return invalid(`tools_changed defers ${spec.name} again`);
  return searched
    ? undefined
    : invalid(`tools_changed loads ${spec.name} without a tool_search`);
}

function sameSpec(a: ToolSpec, b: ToolSpec): boolean {
  const x = specText(a);
  return x !== undefined && x === specText(b);
}

/** The RFC 8785 text of a spec without defer_loading. */
function specText({ defer_loading: _, ...spec }: ToolSpec): string | undefined {
  const json = JsonValue.safeParse(spec);
  const text = json.success ? canonicalize(json.data) : undefined;
  return text?.ok === true ? text.value : undefined;
}
