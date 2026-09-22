import type { z } from "zod";
import { err, ok, type Result } from "../result";

// RFC 8785 (JCS). ECMAScript JSON.stringify already produces the RFC's number spelling
// (§3.2.2.3) and minimal string escapes (§3.2.2.2); JCS adds sorted keys and the rejections.

export type JcsError = { readonly message: string };

type Json = z.core.util.JSONType;

function scalar(value: string | number | boolean | null): string | JcsError {
  if (typeof value === "number" && !Number.isFinite(value)) {
    return { message: "non-finite number" };
  }
  if (typeof value === "string" && !value.isWellFormed()) {
    return { message: "lone surrogate in string" };
  }
  return JSON.stringify(value);
}

function serialize(value: Json): string | JcsError {
  if (value === null || typeof value !== "object") return scalar(value);
  const parts: string[] = [];
  if (Array.isArray(value)) {
    for (const item of value) {
      const text = serialize(item);
      if (typeof text !== "string") return text;
      parts.push(text);
    }
    return `[${parts.join(",")}]`;
  }
  // The default sort compares UTF-16 code units, which is exactly §3.2.3.
  for (const key of Object.keys(value).toSorted()) {
    const item = value[key];
    if (item === undefined) continue;
    const name = scalar(key);
    const text = serialize(item);
    if (typeof name !== "string") return name;
    if (typeof text !== "string") return text;
    parts.push(`${name}:${text}`);
  }
  return `{${parts.join(",")}}`;
}

/** The RFC 8785 canonical text of a JSON value. */
export function canonicalize(value: Json): Result<string, JcsError> {
  const text = serialize(value);
  return typeof text === "string" ? ok(text) : err(text);
}
