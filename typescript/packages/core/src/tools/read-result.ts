import { z } from "zod";
import { type Builtin, builtin, done } from "./builtin";

// read_tool_result: host-side and read_only. It reads a recorded result
// back by call_id: the verified `ref` bytes of a spilled one, else the preview, so a cleared or
// truncated result stays readable from any tool source, also after a fork.

const MAX_LENGTH = 65_536;
const utf8 = new TextEncoder();
const text = new TextDecoder();

export const readToolResult: Builtin = builtin({
  name: "read_tool_result",
  description:
    "Read bytes [offset, offset + length) of an earlier tool result by call_id, including spilled or cleared output.",
  input: z.strictObject({
    call_id: z.string().min(1),
    offset: z.int().min(0),
    length: z.int().min(1).max(MAX_LENGTH),
  }),
  effect: "read_only",
  run: async ({ call_id, offset, length }, _ctx, env) => {
    const recorded = env
      .events()
      .findLast((e) => e.type === "tool_result" && e.data.call_id === call_id);
    if (recorded?.type !== "tool_result")
      return done(`not_found: no result for call ${call_id}`, true);
    const { ref, preview } = recorded.data;
    let bytes: Uint8Array = utf8.encode(preview);
    if (ref !== undefined) {
      const got = env.artifacts.get(ref.sha256);
      if (!got.ok) return done(`${got.error.code}: ${got.error.message}`, true);
      if (got.value.length !== ref.bytes)
        return done(`artifact_corrupt: ${ref.sha256} length`, true);
      bytes = got.value;
    }
    const end = Math.min(bytes.length, offset + length);
    const slice = text.decode(bytes.subarray(Math.min(offset, end), end));
    return done(`[bytes ${offset}-${end} of ${bytes.length}]\n${slice}`);
  },
});
