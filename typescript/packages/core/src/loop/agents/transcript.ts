import { responseText } from "../../fold/state";
import type { KnownEvent } from "../../log";
import { canonicalize } from "../../log/jcs";

// The forwarded transcript of a handoff (spec/schema/README.md, "Handoff scope"): what the user
// and the agent said, and the handing-off turn's tool calls and results, which the target gets
// as untrusted reference. The tool lines are capped at the source's spill threshold, the oldest
// dropped first; the shared vector is spec/conformance/vectors/handoff-transcripts.json.

const encoder = new TextEncoder();
const size = (line: string): number => encoder.encode(line).length;

function toolLine(e: KnownEvent): string | undefined {
  if (e.type === "tool_call" && e.data.name !== "handoff") {
    const input = canonicalize(e.data.input);
    if (!input.ok) throw new Error("a recorded input always canonicalizes");
    return `tool_call ${e.data.call_id} ${e.data.name}: ${input.value}`;
  }
  if (e.type === "tool_result" || e.type === "tool_result_late") {
    const flag = e.data.is_error ? " (error)" : "";
    return `tool_result ${e.data.call_id}${flag}: ${e.data.preview}`;
  }
  return undefined;
}

function spoken(e: KnownEvent): string | undefined {
  if (e.type === "user_input")
    return e.data.text === undefined ? undefined : `user: ${e.data.text}`;
  if (e.type !== "model_response" && e.type !== "model_response_recovered")
    return undefined;
  const said = responseText(e.data.content);
  return said === "" ? undefined : `assistant: ${said}`;
}

export function handoffTranscript(
  events: readonly KnownEvent[],
  capBytes: number,
): string {
  const opener = events.findLastIndex(
    (e) => e.type === "user_input" || e.type === "woken",
  );
  const lines: string[] = [];
  const tools: number[] = [];
  for (const [i, e] of events.entries()) {
    const said = spoken(e);
    const tool = i > opener ? toolLine(e) : undefined;
    if (said !== undefined) lines.push(said);
    else if (tool !== undefined) {
      tools.push(lines.length);
      lines.push(tool);
    }
  }
  let total =
    tools.reduce((n, i) => n + size(lines[i] ?? ""), 0) +
    Math.max(tools.length - 1, 0);
  const dropped: number[] = [];
  while (total > capBytes) {
    const first = tools.shift();
    if (first === undefined) break;
    total -= size(lines[first] ?? "") + (tools.length > 0 ? 1 : 0);
    dropped.push(first);
  }
  const [marker, ...gone] = dropped;
  return lines
    .flatMap((line, i) =>
      i === marker
        ? [`[${dropped.length} earlier tool lines dropped]`]
        : gone.includes(i)
          ? []
          : [line],
    )
    .join("\n");
}
