import { ConfigError } from "./errors";

// secret() (spec/api.json): a reference to a host secret. The object holds only
// the name; the value is read from the host env when host code reveals it, so it never reaches
// the pin, a prompt, the log or the sandbox (invariant 4).

/** Values resolved in this host process, each with the smallest label it was registered under. */
const registered = new Map<string, string>();

/** Compares by Unicode code point, as Python compares strings, so both languages agree. */
function byCodePoints(a: string, b: string): number {
  const x = Array.from(a);
  const y = Array.from(b);
  for (let i = 0; i < Math.min(x.length, y.length); i += 1) {
    const d = (x[i]?.codePointAt(0) ?? 0) - (y[i]?.codePointAt(0) ?? 0);
    if (d !== 0) return d;
  }
  return x.length - y.length;
}

function register(value: string, label: string): void {
  const known = registered.get(value);
  if (known === undefined || byCodePoints(label, known) < 0)
    registered.set(value, label);
}

export class Secret {
  readonly name: string;

  constructor(name: string) {
    this.name = name;
  }

  /** Host code only: the current value. Absent is a setup error, never an empty string. */
  reveal(): string {
    const value = process.env[this.name];
    if (value === undefined || value === "")
      throw new ConfigError("missing_secret", `secret ${this.name} is not set`);
    register(value, this.name);
    return value;
  }

  toString(): string {
    return `secret(${this.name})`;
  }

  toJSON(): { readonly secret: string } {
    return { secret: this.name };
  }
}

export function secret(name: string): Secret {
  return new Secret(name);
}

/**
 * A single-account adapter's credential. Pure: nothing is read until the getter is called, first
 * by the adapter's setup. The first successful call resolves it on the host (an explicit string
 * as given, a Secret, default `secret(env)`, from the host env), registers it for redaction as
 * `<factory>.<option>` and keeps it, so a client made later in the run uses what setup resolved.
 * A failed call keeps nothing and is retried by the next one. Missing or empty is
 * missing_secret naming the option and the variable.
 */
export function credential(
  factory: string,
  option: string,
  value: string | Secret | undefined,
  env: string,
): () => string {
  let kept: string | undefined;
  return () => {
    kept ??= resolve(factory, option, value, env);
    return kept;
  };
}

function resolve(
  factory: string,
  option: string,
  value: string | Secret | undefined,
  env: string,
): string {
  const given = value ?? secret(env);
  const resolved =
    typeof given === "string" ? given : (process.env[given.name] ?? "");
  if (resolved === "")
    throw new ConfigError(
      "missing_secret",
      `${factory}: set ${option} or ${typeof given === "string" ? env : given.name}`,
    );
  register(resolved, `${factory}.${option}`);
  return resolved;
}

/**
 * Replaces every resolved secret value in `text` with its label (the gateway's redaction, C5):
 * longest first, so a value never leaks the tail of a longer one, then by code point.
 */
export function redactSecrets(text: string): string {
  const order = [...registered].toSorted(
    ([a], [b]) =>
      Array.from(b).length - Array.from(a).length || byCodePoints(a, b),
  );
  let out = text;
  for (const [value, label] of order)
    out = out.replaceAll(value, `[secret ${label}]`);
  return out;
}
