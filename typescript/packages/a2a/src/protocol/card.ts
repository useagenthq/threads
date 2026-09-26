import { JsonObject, JsonValue } from "@threads/core/adapter";
import { z } from "zod";
import type { Arr, Opt, Strict } from "./zod";

// `AgentCard` and its security schemes, as JSON. A card is a trust boundary in both directions:
// inbound it is a partner's bytes, outbound it is what we publish. The proto's `oneof scheme` is
// keyed by the set field's own name in JSON (`{"httpAuthSecurityScheme": {...}}`), which is the
// form the pinned spec's own sample card uses.

export const HttpAuthSecurityScheme: Strict<{
  description: Opt<z.ZodString>;
  scheme: z.ZodString;
  bearerFormat: Opt<z.ZodString>;
}> = z.strictObject({
  description: z.string().optional(),
  scheme: z.string().min(1),
  bearerFormat: z.string().optional(),
});
export type HttpAuthSecurityScheme = z.infer<typeof HttpAuthSecurityScheme>;

export const ApiKeySecurityScheme: Strict<{
  description: Opt<z.ZodString>;
  location: z.ZodString;
  name: z.ZodString;
}> = z.strictObject({
  description: z.string().optional(),
  location: z.string().min(1),
  name: z.string().min(1),
});

/**
 * The four schemes we never satisfy stay plain JSON objects: `bearer` is the only auth helper
 * (decision 2), so all we ever ask of these is "is this one of them", and their flows and URLs are
 * a partner's to document. A card that declares one still parses, and `remote_auth_unsupported`
 * names what it asked for.
 */
export const SecurityScheme: Strict<{
  apiKeySecurityScheme: Opt<typeof ApiKeySecurityScheme>;
  httpAuthSecurityScheme: Opt<typeof HttpAuthSecurityScheme>;
  oauth2SecurityScheme: Opt<typeof JsonObject>;
  openIdConnectSecurityScheme: Opt<typeof JsonObject>;
  mtlsSecurityScheme: Opt<typeof JsonObject>;
}> = z.strictObject({
  apiKeySecurityScheme: ApiKeySecurityScheme.optional(),
  httpAuthSecurityScheme: HttpAuthSecurityScheme.optional(),
  oauth2SecurityScheme: JsonObject.optional(),
  openIdConnectSecurityScheme: JsonObject.optional(),
  mtlsSecurityScheme: JsonObject.optional(),
});
export type SecurityScheme = z.infer<typeof SecurityScheme>;

export const SCHEME_KEYS: readonly (keyof SecurityScheme)[] = [
  "apiKeySecurityScheme",
  "httpAuthSecurityScheme",
  "oauth2SecurityScheme",
  "openIdConnectSecurityScheme",
  "mtlsSecurityScheme",
] as const satisfies readonly (keyof SecurityScheme)[];

/** The one scheme a card entry declares; undefined when it declares none or several. */
export function schemeKind(
  scheme: SecurityScheme,
): (typeof SCHEME_KEYS)[number] | undefined {
  const set = SCHEME_KEYS.filter((k) => scheme[k] !== undefined);
  return set.length === 1 ? set[0] : undefined;
}

export const AgentInterface: Strict<{
  url: z.ZodString;
  protocolBinding: z.ZodString;
  tenant: Opt<z.ZodString>;
  protocolVersion: z.ZodString;
}> = z.strictObject({
  url: z.string().min(1),
  protocolBinding: z.string().min(1),
  tenant: z.string().optional(),
  protocolVersion: z.string().min(1),
});
export type AgentInterface = z.infer<typeof AgentInterface>;

export const AgentExtension: Strict<{
  uri: z.ZodString;
  description: Opt<z.ZodString>;
  required: Opt<z.ZodBoolean>;
  params: Opt<typeof JsonObject>;
}> = z.strictObject({
  uri: z.string().min(1),
  description: z.string().optional(),
  required: z.boolean().optional(),
  params: JsonObject.optional(),
});
export type AgentExtension = z.infer<typeof AgentExtension>;

export const AgentCapabilities: Strict<{
  streaming: Opt<z.ZodBoolean>;
  pushNotifications: Opt<z.ZodBoolean>;
  extensions: Opt<Arr<typeof AgentExtension>>;
  extendedAgentCard: Opt<z.ZodBoolean>;
}> = z.strictObject({
  streaming: z.boolean().optional(),
  pushNotifications: z.boolean().optional(),
  extensions: z.array(AgentExtension).optional(),
  extendedAgentCard: z.boolean().optional(),
});
export type AgentCapabilities = z.infer<typeof AgentCapabilities>;

export const AgentSkill: Strict<{
  id: z.ZodString;
  name: z.ZodString;
  description: z.ZodString;
  tags: Arr<z.ZodString>;
  examples: Opt<Arr<z.ZodString>>;
  inputModes: Opt<Arr<z.ZodString>>;
  outputModes: Opt<Arr<z.ZodString>>;
  securityRequirements: Opt<Arr<typeof JsonValue>>;
}> = z.strictObject({
  id: z.string().min(1),
  name: z.string().min(1),
  description: z.string().min(1),
  // tags is REQUIRED in the proto, so a card without it is a parse error and we always emit it.
  tags: z.array(z.string()),
  examples: z.array(z.string()).optional(),
  inputModes: z.array(z.string()).optional(),
  outputModes: z.array(z.string()).optional(),
  securityRequirements: z.array(JsonValue).optional(),
});
export type AgentSkill = z.infer<typeof AgentSkill>;

export const AgentProvider: Strict<{
  url: z.ZodString;
  organization: z.ZodString;
}> = z.strictObject({
  url: z.string().min(1),
  organization: z.string().min(1),
});

export const AgentCardSignature: Strict<{
  protected: z.ZodString;
  signature: z.ZodString;
  header: Opt<typeof JsonObject>;
}> = z.strictObject({
  protected: z.string().min(1),
  signature: z.string().min(1),
  header: JsonObject.optional(),
});

/**
 * Two fields are read loosely on purpose. `securityRequirements` is the proto's name for field 9,
 * while the spec's own sample card writes it `security` with a shape the proto does not describe
 * (`[{"google": ["openid"]}]` against `{"schemes": {...}}`); we accept either, store neither, and
 * record the discrepancy in spec/schema/a2a/README.md. Nothing we do depends on it: our
 * `authenticate` is what checks a caller, and `bearer` is the only credential we send.
 */
export const AgentCard: Strict<{
  name: z.ZodString;
  description: z.ZodString;
  supportedInterfaces: Arr<typeof AgentInterface>;
  provider: Opt<typeof AgentProvider>;
  version: z.ZodString;
  documentationUrl: Opt<z.ZodString>;
  capabilities: typeof AgentCapabilities;
  securitySchemes: Opt<z.ZodRecord<z.ZodString, typeof SecurityScheme>>;
  securityRequirements: Opt<Arr<typeof JsonValue>>;
  security: Opt<Arr<typeof JsonValue>>;
  defaultInputModes: Arr<z.ZodString>;
  defaultOutputModes: Arr<z.ZodString>;
  skills: Arr<typeof AgentSkill>;
  signatures: Opt<Arr<typeof AgentCardSignature>>;
  iconUrl: Opt<z.ZodString>;
}> = z.strictObject({
  name: z.string().min(1),
  description: z.string().min(1),
  supportedInterfaces: z.array(AgentInterface).min(1),
  provider: AgentProvider.optional(),
  version: z.string().min(1),
  documentationUrl: z.string().optional(),
  capabilities: AgentCapabilities,
  securitySchemes: z.record(z.string(), SecurityScheme).optional(),
  securityRequirements: z.array(JsonValue).optional(),
  security: z.array(JsonValue).optional(),
  defaultInputModes: z.array(z.string()),
  defaultOutputModes: z.array(z.string()),
  skills: z.array(AgentSkill),
  signatures: z.array(AgentCardSignature).optional(),
  iconUrl: z.string().optional(),
});
export type AgentCard = z.infer<typeof AgentCard>;
