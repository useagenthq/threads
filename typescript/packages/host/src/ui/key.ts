import {
  derivedThreadId,
  type Principal,
  principalKey,
  type ThreadId,
} from "@threads/core/host";

// A browser's chat key names one thread per (principal, agent, key), by derivation: no lookup
// table, and the key itself is never recorded (spec/schema/ui/README.md, "Chat key").
// thread_id = UUIDv8 of sha256(lp("threads-ui-v1") ‖ lp(principalKey) ‖ lp(agent) ‖ lp(key)).

export const CHAT_KEY: RegExp = /^[A-Za-z0-9_.-]{1,128}$/;

const DOMAIN = "threads-ui-v1";

export function uiThreadId(
  principal: Principal,
  agent: string,
  key: string,
): ThreadId {
  return derivedThreadId(DOMAIN, [principalKey(principal), agent, key]);
}
