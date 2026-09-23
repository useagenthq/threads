import { z } from "zod";
import type { ToolRun } from "../../loop/types";
import type { LookupResult } from "../../model";
import { type Builtin, builtin, done } from "../builtin";
import { OpenPullRequestInput } from "../gateway-inputs";
import { missingCredential } from "./clone";
import type { GitOptions } from "./host";

// open_pull_request: reconcilable, looked up by head branch on the forge
// (GitHub's REST API). One pull request per head: an existing one is the answer, never a
// second. The credential is sent only from the host.

type Input = z.infer<typeof OpenPullRequestInput>;

// The forge's answers are network responses: parsed at the boundary.
const Pull = z.object({ html_url: z.string(), number: z.int() });
const Pulls = z.array(Pull);

function forge(
  o: GitOptions,
  path: string,
  init: RequestInit,
): Promise<Response> {
  const base = (o.apiUrl ?? "https://api.github.com").replace(/\/$/, "");
  const send = o.fetch ?? ((url: string, i: RequestInit) => fetch(url, i));
  return send(`${base}${path}`, {
    ...init,
    headers: {
      accept: "application/vnd.github+json",
      authorization: `Bearer ${o.credential.reveal()}`,
      "content-type": "application/json",
      "user-agent": "threads-git-gateway/1",
    },
  });
}

const said = (pull: z.infer<typeof Pull>): string =>
  `pull request #${pull.number}: ${pull.html_url}`;

/** The pull request whose head is `input.head`, open or closed. */
async function byHead(
  o: GitOptions,
  input: Input,
): Promise<LookupResult<string>> {
  const owner = input.repo.split("/")[0] ?? "";
  const query = new URLSearchParams({
    head: `${owner}:${input.head}`,
    state: "all",
  });
  const res = await forge(o, `/repos/${input.repo}/pulls?${query}`, {
    method: "GET",
  });
  if (!res.ok)
    return { status: "unknown", reason: `the forge answered ${res.status}` };
  const pulls = Pulls.safeParse(await res.json());
  if (!pulls.success)
    return { status: "unknown", reason: "the forge answered malformed pulls" };
  const first = pulls.data[0];
  return first === undefined
    ? { status: "not_found" }
    : { status: "found", value: said(first) };
}

async function open(o: GitOptions, input: Input): Promise<ToolRun> {
  const res = await forge(o, `/repos/${input.repo}/pulls`, {
    method: "POST",
    body: JSON.stringify({
      title: input.title,
      head: input.head,
      base: input.base,
      ...(input.body === undefined ? {} : { body: input.body }),
    }),
  });
  if (res.status === 201) {
    const pull = Pull.safeParse(await res.json());
    // Created, but the answer is unreadable: settle it by lookup, never by resending.
    if (!pull.success) return { kind: "unknown", reason: "transport_error" };
    return {
      kind: "done",
      output: said(pull.data),
      isError: false,
      receipt: String(pull.data.number),
    };
  }
  if (res.status === 422) {
    const existing = await byHead(o, input);
    if (existing.status === "found")
      return done(`already open: ${existing.value}`);
  }
  // A server error may have created it: settled by lookup.
  if (res.status >= 500) return { kind: "unknown", reason: "transport_error" };
  // Any other answer is the forge refusing: nothing was created.
  return done(
    `the forge refused the pull request (${res.status}): ${await res.text()}`,
    true,
  );
}

export function openPullRequest(o: GitOptions): Builtin {
  return builtin({
    name: "open_pull_request",
    input: OpenPullRequestInput,
    effect: "reconcilable",
    run: async (input, ctx) => {
      const missing = missingCredential(o);
      if (missing !== undefined) return missing;
      const fenced = await ctx.fence();
      if (!fenced.ok) return { kind: "not_sent" };
      return open(o, input);
    },
    // No pull request for the head means none was created: not_found is final.
    reconcile: () => ({
      finality: "final",
      lookup: async (_key, raw) => {
        const input = OpenPullRequestInput.safeParse(raw);
        return input.success
          ? byHead(o, input.data)
          : {
              status: "unknown",
              reason: "the call's input is not open_pull_request's",
            };
      },
    }),
  });
}
