import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { ErrorCode, LogLine } from "../src/log";

// Exports the Zod log schema as JSON Schema. Anything Zod can't represent throws.

const OUT = join(import.meta.dir, "../generated/events.v1.from-zod.json");

const line = z.toJSONSchema(LogLine, {
  target: "draft-2020-12",
  unrepresentable: "throw",
});
// ErrorCode is not part of a line, but it is part of the wire contract.
const { ErrorCode: errorCode } =
  z.toJSONSchema(ErrorCode, {
    target: "draft-2020-12",
    unrepresentable: "throw",
  }).$defs ?? {};
const schema = {
  ...line,
  $id: "urn:threads:schema:events:v1",
  $defs: { ...line.$defs, ErrorCode: errorCode },
};

mkdirSync(join(OUT, ".."), { recursive: true });
writeFileSync(OUT, `${JSON.stringify(schema, null, 2)}\n`);
console.log(`wrote ${OUT}`);
