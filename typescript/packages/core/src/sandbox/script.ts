import { z } from "zod";
import type { EnumOf, Opt, Strict } from "../log/zod-types";

// case.schema.json $defs/SandboxScript: the fake provider's scripted behaviour. Parsed strictly,
// so an unknown key fails instead of being ignored.

type Rec<T extends z.core.SomeType> = z.ZodRecord<z.ZodString, T>;

const ScriptedTool: Strict<{
  output: z.ZodString;
  is_error: Opt<z.ZodBoolean>;
  executed_keys: Opt<Rec<z.ZodString>>;
  lookup: Opt<
    Rec<
      Strict<{
        result: EnumOf<["found", "not_found"]>;
        final: z.ZodBoolean;
        output: Opt<z.ZodString>;
      }>
    >
  >;
  process: Opt<EnumOf<["running", "terminated", "unknown"]>>;
}> = z.strictObject({
  output: z.string(),
  is_error: z.boolean().optional(),
  executed_keys: z.record(z.string(), z.string()).optional(),
  lookup: z
    .record(
      z.string(),
      z.strictObject({
        result: z.enum(["found", "not_found"]),
        final: z.boolean(),
        output: z.string().optional(),
      }),
    )
    .optional(),
  process: z.enum(["running", "terminated", "unknown"]).optional(),
});

const ScriptedSnapshot: Strict<{
  restore_sandbox_id: z.ZodString;
  restore_response: Opt<EnumOf<["ok", "lost"]>>;
  create_lookup: Opt<EnumOf<["found", "unsupported"]>>;
}> = z.strictObject({
  restore_sandbox_id: z.string(),
  restore_response: z.enum(["ok", "lost"]).optional(),
  create_lookup: z.enum(["found", "unsupported"]).optional(),
});

export const SandboxScript: Strict<{
  tools: Opt<Rec<typeof ScriptedTool>>;
  snapshots: Opt<Rec<typeof ScriptedSnapshot>>;
}> = z.strictObject({
  tools: z.record(z.string(), ScriptedTool).optional(),
  snapshots: z.record(z.string(), ScriptedSnapshot).optional(),
});
export type SandboxScript = z.infer<typeof SandboxScript>;
