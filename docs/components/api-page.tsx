"use client";
import { createOpenAPIPage } from "fumadocs-openapi/ui";

type MediaAdapters = NonNullable<NonNullable<Parameters<typeof createOpenAPIPage>[0]>["mediaAdapters"]>;

const asText = (body: unknown): string => (typeof body === "string" ? body : JSON.stringify(body));

// Channel webhooks take the provider's raw bytes (`*/*`): send them through unchanged.
const mediaAdapters: MediaAdapters = {
  "*/*": {
    encode: ({ body }) => asText(body),
    generateExample: ({ body }, ctx) =>
      ctx.lang === "js" ? `const body = ${JSON.stringify(asText(body))};` : undefined,
  },
};

export const OpenAPIPage = createOpenAPIPage({ mediaAdapters });
