import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { canonicalize } from "@threads/core/host";
import { uiMessageChunkSchema } from "ai";
import { z } from "zod";

// bun scripts/export-ui-schemas.ts          write spec/schema/ui/ai-sdk-ui.v1.schema.json
// bun scripts/export-ui-schemas.ts --check  fail if the committed file differs from the export
//
// Dev only (ai is a dev dependency). The AI SDK UI message chunk schema of the pinned `ai`, as
// the SDK itself converts it to JSON Schema, so Python validates frames against the same shape
// the stock client parses them with. Its one custom type (the `data-*` chunk type) can't be
// converted, so it is written as the string pattern the custom check applies.

export const AI_SDK_SCHEMA: string = join(
  import.meta.dir,
  "../../../../spec/schema/ui/ai-sdk-ui.v1.schema.json",
);
const PINNED = "7.0.113";

/** The export, as the bytes the committed file holds. */
export async function aiSdkSchema(): Promise<string> {
  // Every z.custom instance shares its internals' prototype, which toJSONSchema reads first.
  const internals: object = Object.getPrototypeOf(z.custom<string>()._zod);
  Object.defineProperty(internals, "toJSONSchema", {
    configurable: true,
    value: () => ({ type: "string", pattern: "^data-" }),
  });
  try {
    const exported = await uiMessageChunkSchema().jsonSchema;
    const doc = {
      ...z.record(z.string(), z.unknown()).parse(exported),
      $id: "urn:threads:schema:ui:ai-sdk:v1",
      title: `AI SDK UI message stream v1 chunk (ai@${PINNED})`,
      description: `Exported from uiMessageChunkSchema of ai@${PINNED} by typescript/packages/host/scripts/export-ui-schemas.ts. Never edited by hand.`,
    };
    const text = canonicalize(doc);
    if (!text.ok) throw new Error(text.error.message);
    return `${JSON.stringify(JSON.parse(text.value), null, 2)}\n`;
  } finally {
    Reflect.deleteProperty(internals, "toJSONSchema");
  }
}

if (import.meta.main) {
  const bytes = await aiSdkSchema();
  if (process.argv[2] === "--check") {
    if (readFileSync(AI_SDK_SCHEMA, "utf8") !== bytes) {
      console.error(`${AI_SDK_SCHEMA} differs from the ai@${PINNED} export`);
      process.exitCode = 1;
    }
  } else {
    writeFileSync(AI_SDK_SCHEMA, bytes);
    console.log(`wrote ${AI_SDK_SCHEMA}`);
  }
}
