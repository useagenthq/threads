import { ConfigError } from "@threads/core";
import { SEMCONV } from "./attrs";

// Configuration: an option, then the traces variable, then the generic one, then the default
// (spec/otel/README.md, "Configuration"; pinned by spec/otel/vectors/otel-env.json).

export type Options = {
  readonly endpoint?: string;
  readonly headers?: Readonly<Record<string, string>>;
  readonly service?: string;
};

export type Config = {
  readonly endpoint: string;
  readonly headers: Readonly<Record<string, string>>;
  readonly timeoutMs: number;
  readonly compression: "none" | "gzip";
  readonly resource: Readonly<Record<string, string>>;
};

export type Env = Readonly<Record<string, string | undefined>>;

/** A refused setting: its message names the option or variable, never a header value. */
function refused(message: string): Error {
  return new ConfigError("invalid_config", `otel(): ${message}`);
}

const T = "OTEL_EXPORTER_OTLP_TRACES_";
const G = "OTEL_EXPORTER_OTLP_";
const UNSUPPORTED = ["CERTIFICATE", "CLIENT_CERTIFICATE", "CLIENT_KEY"];

/** The first set variable of the traces and the generic form of `name`. */
function pick(
  env: Env,
  name: string,
): { readonly name: string; readonly value: string } | undefined {
  for (const variable of [`${T}${name}`, `${G}${name}`]) {
    const value = env[variable];
    if (value !== undefined && value !== "") return { name: variable, value };
  }
  return undefined;
}

function decode(text: string, variable: string): string {
  try {
    return decodeURIComponent(text);
  } catch {
    throw refused(`${variable} has an invalid percent escape`);
  }
}

/** `k=v,k=v`, split on "," then the first "=", trimmed and percent-decoded. */
export function parseList(
  text: string,
  variable: string,
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const entry of text.split(",")) {
    if (entry.trim() === "") continue;
    const at = entry.indexOf("=");
    const key = at < 0 ? "" : decode(entry.slice(0, at).trim(), variable);
    if (key === "")
      throw refused(
        `${variable}: every entry must be key=value with a non-empty key`,
      );
    out[key] = decode(entry.slice(at + 1).trim(), variable);
  }
  return out;
}

function endpoint(options: Options, env: Env): string {
  if (options.endpoint !== undefined) return options.endpoint;
  const traces = env[`${T}ENDPOINT`];
  if (traces !== undefined && traces !== "") return traces;
  const generic = env[`${G}ENDPOINT`];
  if (generic !== undefined && generic !== "")
    return `${generic.replace(/\/+$/, "")}/v1/traces`;
  throw refused(
    `no collector endpoint: pass otel({endpoint}) or set ${T}ENDPOINT or ${G}ENDPOINT`,
  );
}

function checked(env: Env): Pick<Config, "timeoutMs" | "compression"> {
  const protocol = pick(env, "PROTOCOL");
  if (protocol !== undefined && protocol.value !== "http/json")
    throw refused(
      `${protocol.name}=${protocol.value}: only http/json is supported`,
    );
  const timeout = pick(env, "TIMEOUT");
  const ms = timeout === undefined ? 10_000 : Number(timeout.value);
  if (timeout !== undefined && (!/^\d+$/.test(timeout.value) || ms <= 0))
    throw refused(`${timeout.name} must be a positive number of milliseconds`);
  const compression = pick(env, "COMPRESSION");
  const value = compression?.value ?? "none";
  if (value !== "none" && value !== "gzip")
    throw refused(`${compression?.name}: compression must be none or gzip`);
  return { timeoutMs: ms, compression: value };
}

function resource(
  options: Options,
  env: Env,
  language: string,
): Record<string, string> {
  const variable = env["OTEL_RESOURCE_ATTRIBUTES"];
  const attrs = variable ? parseList(variable, "OTEL_RESOURCE_ATTRIBUTES") : {};
  const named = env["OTEL_SERVICE_NAME"];
  return {
    ...attrs,
    "service.name":
      options.service ??
      (named || undefined) ??
      attrs["service.name"] ??
      "threads",
    "telemetry.sdk.name": "threads",
    "telemetry.sdk.language": language,
    "threads.otel.semconv": SEMCONV,
  };
}

/** The exporter's settings; a refusal (invalid_config) names what to fix. */
export function resolve(options: Options, env: Env, language: string): Config {
  for (const name of UNSUPPORTED)
    for (const variable of [`${T}${name}`, `${G}${name}`])
      if (env[variable])
        throw refused(
          `${variable} is set, but client certificates are not supported: put an OpenTelemetry Collector in front`,
        );
  const url = endpoint(options, env);
  const settings = checked(env);
  const headers = pick(env, "HEADERS");
  return {
    endpoint: url,
    headers:
      options.headers ??
      (headers ? parseList(headers.value, headers.name) : {}),
    ...settings,
    resource: resource(options, env, language),
  };
}
