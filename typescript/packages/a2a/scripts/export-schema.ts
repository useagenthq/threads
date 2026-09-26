import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { canonicalize } from "@threads/core/host";
import { z } from "zod";
import {
  AgentCapabilities,
  AgentCard,
  AgentCardSignature,
  AgentExtension,
  AgentInterface,
  AgentProvider,
  AgentSkill,
  ApiKeySecurityScheme,
  HttpAuthSecurityScheme,
  SecurityScheme,
} from "../src/protocol/card";
import {
  CancelTaskRequest,
  GetTaskRequest,
  ListTasksRequest,
  ListTasksResponse,
  SendMessageConfiguration,
  SendMessageRequest,
  SubscribeToTaskRequest,
} from "../src/protocol/requests";
import {
  Artifact,
  Message,
  Part,
  Role,
  SendMessageResponse,
  StreamResponse,
  Task,
  TaskArtifactUpdateEvent,
  TaskState,
  TaskStatus,
  TaskStatusUpdateEvent,
} from "../src/protocol/task";

// bun scripts/export-schema.ts           write spec/schema/a2a.v1.schema.json
// bun scripts/export-schema.ts --check   fail if the committed file differs; list every difference
//
// Our A2A subset as one JSON Schema document: a `$def` per message we parse, exported from the Zod
// source in src/protocol/. Each `$def` is named after its message in the vendored proto
// (spec/schema/a2a/a2a.proto), which is what lets spec/tools/check_a2a_pin.py compare the two
// field by field. Python's boundary models are generated from this file.

type Json = z.core.util.JSONType;
type Node = { [key: string]: Json };

const SPEC = join(
  import.meta.dir,
  "../../../../spec/schema/a2a.v1.schema.json",
);

/**
 * Every message we parse, keyed by its proto name. Two Zod names differ from the proto's, which
 * spells initialisms in caps (`APIKeySecurityScheme`, `HTTPAuthSecurityScheme`); the proto name
 * wins here, because the pin checker reads these keys as message names.
 */
const MESSAGES: Readonly<Record<string, z.core.$ZodType>> = {
  AgentCapabilities,
  AgentCard,
  AgentCardSignature,
  AgentExtension,
  AgentInterface,
  AgentProvider,
  AgentSkill,
  APIKeySecurityScheme: ApiKeySecurityScheme,
  Artifact,
  CancelTaskRequest,
  GetTaskRequest,
  HTTPAuthSecurityScheme: HttpAuthSecurityScheme,
  ListTasksRequest,
  ListTasksResponse,
  Message,
  Part,
  Role,
  SecurityScheme,
  SendMessageConfiguration,
  SendMessageRequest,
  SendMessageResponse,
  StreamResponse,
  SubscribeToTaskRequest,
  Task,
  TaskArtifactUpdateEvent,
  TaskState,
  TaskStatus,
  TaskStatusUpdateEvent,
};

/** RFC 8785 key order, two-space indent: stable bytes that still read well in a diff. */
function format(value: Json): string {
  const canonical = canonicalize(value);
  if (!canonical.ok) throw new Error(canonical.error.message);
  return `${JSON.stringify(JSON.parse(canonical.value), null, 2)}\n`;
}

/** Every JSON pointer where `a` and `b` differ. */
function differences(a: Json, b: Json, path = ""): string[] {
  if (isNode(a) && isNode(b)) {
    const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
    return [...keys].toSorted().flatMap((key) => {
      const at = `${path}/${key}`;
      if (!(key in a)) return [`${at}: only in the committed file`];
      if (!(key in b)) return [`${at}: only in the export`];
      return differences(a[key] ?? null, b[key] ?? null, at);
    });
  }
  if (Array.isArray(a) && Array.isArray(b) && a.length === b.length) {
    return a.flatMap((item, i) =>
      differences(item, b[i] ?? null, `${path}/${i}`),
    );
  }
  return JSON.stringify(a) === JSON.stringify(b)
    ? []
    : [
        `${path || "/"}: export ${JSON.stringify(a)} ≠ committed ${JSON.stringify(b)}`,
      ];
}

function isNode(value: Json): value is Node {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * The subset as one document. Registering the ids here rather than in src/protocol keeps the
 * protocol source free of export concerns; `$defs` is what Zod resolves the refs to.
 */
export function a2aSchema(): Node {
  for (const [id, schema] of Object.entries(MESSAGES))
    z.globalRegistry.add(schema, { id });
  const exported = z.toJSONSchema(z.tuple(Object.values(MESSAGES)), {
    target: "draft-2020-12",
    io: "input",
    unrepresentable: "throw",
  });
  const tree: Json = JSON.parse(JSON.stringify(exported));
  const { $defs: defs } = isNode(tree) ? tree : {};
  if (!isNode(defs)) throw new Error("export has no $defs");
  const missing = Object.keys(MESSAGES).filter((name) => !(name in defs));
  if (missing.length > 0)
    throw new Error(`not exported as a $def: ${missing.join(", ")}`);
  return {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "urn:threads:schema:a2a:v1",
    title: "A2A 1.0, the subset threads parses",
    description:
      "Exported from the Zod source in typescript/packages/a2a/src/protocol/ by typescript/packages/a2a/scripts/export-schema.ts. Never edited by hand. Each $def is named after its message in spec/schema/a2a/a2a.proto.",
    $defs: defs,
  };
}

function check(schema: Json, file: string): number {
  const committed = readFileSync(file, "utf8");
  if (committed === format(schema)) return 0;
  const found = differences(schema, JSON.parse(committed));
  for (const line of found) console.error(line);
  console.error(
    found.length === 0
      ? `${file}: same schema, not in canonical form; run bun run schema:export`
      : `${file}: ${found.length} differences from the Zod export; run bun run schema:export`,
  );
  return 1;
}

if (import.meta.main) {
  const schema = a2aSchema();
  if (process.argv[2] === "--check") {
    process.exitCode = check(schema, SPEC);
  } else {
    writeFileSync(SPEC, format(schema));
    console.log(`wrote ${SPEC}`);
  }
}
