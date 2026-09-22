import type { z } from "zod";
import { err, ok, type Result } from "../result";
import { MAX_DEPTH } from "./json";

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

/** `depth` is the level `value` occupies if it is an array or object; the root is 1. */
function serialize(value: Json, depth: number): string | JcsError {
  if (value === null || typeof value !== "object") return scalar(value);
  if (depth > MAX_DEPTH)
    return { message: `nesting deeper than ${MAX_DEPTH} levels` };
  return Array.isArray(value)
    ? serializeArray(value, depth)
    : serializeObject(value, depth);
}

function serializeArray(items: Json[], depth: number): string | JcsError {
  const parts: string[] = [];
  for (const item of items) {
    const text = serialize(item, depth + 1);
    if (typeof text !== "string") return text;
    parts.push(text);
  }
  return `[${parts.join(",")}]`;
}

function serializeObject(
  obj: { [key: string]: Json },
  depth: number,
): string | JcsError {
  const parts: string[] = [];
  // The default sort compares UTF-16 code units, which is exactly §3.2.3.
  for (const key of Object.keys(obj).toSorted()) {
    const item = obj[key];
    if (item === undefined) continue;
    const name = scalar(key);
    const text = serialize(item, depth + 1);
    if (typeof name !== "string") return name;
    if (typeof text !== "string") return text;
    parts.push(`${name}:${text}`);
  }
  return `{${parts.join(",")}}`;
}

/** The RFC 8785 canonical text of a JSON value. */
export function canonicalize(value: Json): Result<string, JcsError> {
  const text = serialize(value, 1);
  return typeof text === "string" ? ok(text) : err(text);
}
