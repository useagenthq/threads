import { createHmac } from "node:crypto";
import { type RawRequest, secret } from "@threads/core/adapter";
import { github } from "../src";

export const WEBHOOK = "whsec-test";
export const TOKEN = "ghs_testtoken_do_not_leak";
process.env["THREADS_GH_TEST_WEBHOOK"] = WEBHOOK;
process.env["THREADS_GH_TEST_TOKEN"] = TOKEN;

export const CHALLENGE = "0192e000-0000-7000-8000-00000000c0de";

export const adapter: ReturnType<typeof github> = github({
  agent: "triage",
  webhookSecret: secret("THREADS_GH_TEST_WEBHOOK"),
  token: secret("THREADS_GH_TEST_TOKEN"),
  appSlug: "threads-bot",
});

type Sender = { id: number; login: string; type: string };
export const alice: Sender = { id: 42, login: "alice", type: "User" };

export function commentPayload(
  body: string,
  sender: Sender = alice,
): Record<string, unknown> {
  return {
    action: "created",
    installation: { id: 99 },
    repository: { full_name: "acme/app" },
    issue: { number: 7 },
    comment: { id: 1, body, user: sender },
    sender,
  };
}

export function request(
  payload: unknown,
  event = "issue_comment",
  overrides: Record<string, string | undefined> = {},
): RawRequest {
  const body = new TextEncoder().encode(JSON.stringify(payload));
  const headers: Record<string, string> = {
    "x-hub-signature-256": `sha256=${createHmac("sha256", WEBHOOK).update(body).digest("hex")}`,
    "x-github-delivery": "d-1",
    "x-github-event": event,
  };
  for (const [name, value] of Object.entries(overrides))
    if (value === undefined) delete headers[name];
    else headers[name] = value;
  return { headers, body };
}

export type Call = { readonly url: string; readonly init: RequestInit };

/** A fake transport: records each request and answers from `respond`. */
export function fakeFetch(
  respond: (call: Call) => Response | Promise<Response>,
): {
  readonly calls: Call[];
  readonly fetch: (
    input: string | URL | Request,
    init?: RequestInit,
  ) => Promise<Response>;
} {
  const calls: Call[] = [];
  return {
    calls,
    fetch: async (input, init = {}) => {
      const call = { url: String(input), init };
      calls.push(call);
      return respond(call);
    },
  };
}

export function json(
  status: number,
  body: unknown,
  headers: Record<string, string> = {},
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}
