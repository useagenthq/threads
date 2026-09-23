import { createOpenAPI } from "fumadocs-openapi/server";

// Bundled from spec/schema/host-api/openapi.json by scripts/gen_api_ref.py.
export const openapi = createOpenAPI({
  input: ["./openapi.json"],
});
