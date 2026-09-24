import type { HookRecord } from "./files";

// The stand-ins' order (spec lane 22, A.2). The log records no extension list, so it is
// recovered from the records: where several extensions decided at one hook point, before*
// hooks ran them in declaration order and after* hooks in reverse. The first shared point of
// each pair decides it; extensions that never share a point go by name, so both runtimes agree.

/** Runs of consecutive records at one hook point: the same hook and the same call. */
function hookPoints(records: readonly HookRecord[]): readonly HookRecord[][] {
  const call = (x: HookRecord) =>
    x.at !== undefined && "call_id" in x.at ? x.at.call_id : "";
  const points: HookRecord[][] = [];
  for (const r of records) {
    const last = points.at(-1);
    const head = last?.[0];
    if (last !== undefined && head?.hook === r.hook && call(head) === call(r))
      last.push(r);
    else points.push([r]);
  }
  return points;
}

/** Each extension's predecessors: the extensions declared before it. */
function precedences(
  records: readonly HookRecord[],
): ReadonlyMap<string, ReadonlySet<string>> {
  const before = new Map<string, Set<string>>();
  const decided = new Set<string>();
  for (const point of hookPoints(records)) {
    const ran = [...new Set(point.map((r) => r.extension))];
    const declared = point[0]?.hook.startsWith("after_")
      ? ran.toReversed()
      : ran;
    for (const [i, a] of declared.entries())
      for (const b of declared.slice(i + 1)) {
        if (decided.has(`${a}\n${b}`) || decided.has(`${b}\n${a}`)) continue;
        decided.add(`${a}\n${b}`);
        before.set(b, new Set([...(before.get(b) ?? []), a]));
      }
  }
  return before;
}

/** Extension names in the order that reproduces the recorded order at shared hook points. */
export function standInOrder(
  records: readonly HookRecord[],
): readonly string[] {
  const names = [...new Set(records.map((r) => r.extension))].toSorted();
  const before = precedences(records);
  const out: string[] = [];
  const placed = (n: string) => out.includes(n);
  while (out.length < names.length) {
    const ready = names.find(
      (n) => !placed(n) && [...(before.get(n) ?? [])].every(placed),
    );
    // A cycle can't come from one loop's records; fall back to the name order.
    out.push(ready ?? names.find((n) => !placed(n)) ?? "");
  }
  return out;
}
