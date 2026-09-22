import { createHash } from "node:crypto";

/** Lowercase hex SHA-256 (wire rule 3). A string is hashed as its UTF-8 bytes. */
export function sha256Hex(input: Uint8Array | string): string {
  return createHash("sha256").update(input).digest("hex");
}
