import { ConfigError } from "./errors";

// secret() (spec/api.json, ): a reference to a host secret. The object holds only
// the name; the value is read from the host env when host code reveals it, so it never reaches
// the pin, a prompt, the log or the sandbox (invariant 4).

/** Values revealed in this host process, by value, so results can be scrubbed (C5). */
const revealed = new Map<string, string>();

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
    revealed.set(value, this.name);
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

/** Replaces every revealed secret value in `text` with its name (the gateway's redaction, C5). */
export function redactSecrets(text: string): string {
  let out = text;
  for (const [value, name] of revealed)
    out = out.replaceAll(value, `[secret ${name}]`);
  return out;
}
