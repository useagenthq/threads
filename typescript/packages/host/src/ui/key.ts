import {
  type Principal,
  principalKey,
  sha256Hex,
  ThreadId,
} from "@threads/core/host";

// A browser's chat key names one thread per (principal, agent, key), by derivation: no lookup
// table, and the key itself is never recorded (spec/schema/ui/README.md, "Chat key").
// thread_id = UUIDv8 of sha256(lp("threads-ui-v1") ‖ lp(principalKey) ‖ lp(agent) ‖ lp(key)).

export const CHAT_KEY: RegExp = /^[A-Za-z0-9_.-]{1,128}$/;

/** 4-byte big-endian UTF-8 length, then the bytes: no two field splits collide. */
function lp(text: string): Uint8Array {
  const bytes = new TextEncoder().encode(text);
  const out = new Uint8Array(4 + bytes.length);
  new DataView(out.buffer).setUint32(0, bytes.length);
  out.set(bytes, 4);
  return out;
}

export function uiThreadId(
  principal: Principal,
  agent: string,
  key: string,
): ThreadId {
  const fields = ["threads-ui-v1", principalKey(principal), agent, key].map(lp);
  const input = new Uint8Array(fields.reduce((n, f) => n + f.length, 0));
  let at = 0;
  for (const f of fields) {
    input.set(f, at);
    at += f.length;
  }
  const hex = sha256Hex(input).slice(0, 32);
  const version = `8${hex.slice(13, 16)}`;
  const variant = (
    (Number.parseInt(hex.slice(16, 17), 16) & 0x3) |
    0x8
  ).toString(16);
  return ThreadId.parse(
    `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${version}-${variant}${hex.slice(17, 20)}-${hex.slice(20, 32)}`,
  );
}
