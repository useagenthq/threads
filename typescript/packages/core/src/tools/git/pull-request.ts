import { z } from "zod";
import type { ToolRun } from "../../loop/types";
import type { LookupResult } from "../../model";
import { type Builtin, builtin, done } from "../builtin";
import { OpenPullRequestInput } from "../gateway-inputs";
import { liveTransport, vet } from "../web-transport";
import { missingCredential } from "./clone";
import type { GitOptions } from "./host";

// open_pull_request (spec/schema/README.md "Git gateway"): reconcilable. The
// forge (GitHub's REST API) is asked for a pull request for (head, base) in every state before
// anything is created, so a lost create that was later closed never leads to a second one. The
// credential is sent only from the host.

type Input = z.infer<typeof OpenPullRequestInput>;

// The forge's answers are network responses: parsed at the boundary.
const Pull = z.object({
  html_url: z.string(),
  number: z.int(),
  state: z.enum(["open", "closed"]),
  merged_at: z.string().nullable().optional(),
});
const Pulls = z.array(Pull);
// GitHub's 422 when a pull request for the head already exists.
const Exists = z.object({
  errors: z.array(z.object({ message: z.string().optional() })).optional(),
});

/** The live forge API: the api_url's resolved addresses must pass the SSRF guard. */
export async function forgeFetch(
  url: string,
  init: RequestInit,
): Promise<Response> {
  const target = await vet(new URL(url), liveTransport);
  // Refused before any connection: a forge answer the tool reports as a refusal.
  if ("denied" in target)
    return new Response(`forge api_url refused: ${target.denied}`, {
      status: 403,
    });
  return liveTransport.fetch(url, target.address, {
    method: init.method === "POST" ? "POST" : "GET",
    headers: Object.fromEntries(new Headers(init.headers)),
    ...(typeof init.body === "string" ? { body: init.body } : {}),
    signal: init.signal ?? AbortSignal.timeout(60_000),
  });
}

function forge(
  o: GitOptions,
  path: string,
  init: RequestInit,
): Promise<Response> {
  const base = (o.apiUrl ?? "https://api.github.com").replace(/\/$/, "");
  return (o.fetch ?? forgeFetch)(`${base}${path}`, {
    ...init,
    headers: {
      accept: "application/vnd.github+json",
      authorization: `Bearer ${o.credential.reveal()}`,
      "content-type": "application/json",
      "user-agent": "threads-git-gateway/1",
    },
  });
}

const said = (pull: z.infer<typeof Pull>): string => {
  const state = pull.merged_at ? "merged" : pull.state;
  return `pull request #${pull.number} (${state}): ${pull.html_url}`;
};

/** The newest pull request for (head, base), in any state. */
async function existing(
  o: GitOptions,
  input: Input,
): Promise<LookupResult<string>> {
  const owner = input.repo.split("/")[0] ?? "";
  const query = new URLSearchParams({
    head: `${owner}:${input.head}`,
    base: input.base,
    state: "all",
    sort: "created",
    direction: "desc",
  });
  const res = await forge(o, `/repos/${input.repo}/pulls?${query}`, {
    method: "GET",
  });
  if (!res.ok)
    return { status: "unknown", reason: `the forge answered ${res.status}` };
  const pulls = Pulls.safeParse(await res.json());
  if (!pulls.success)
    return { status: "unknown", reason: "the forge answered malformed pulls" };
  const newest = pulls.data[0];
  return newest === undefined
    ? { status: "not_found" }
    : { status: "found", value: said(newest) };
}

const alreadyExists = (body: string): boolean => {
  try {
    const parsed = Exists.safeParse(JSON.parse(body));
    return (
      parsed.success &&
      (parsed.data.errors ?? []).some((e) =>
        e.message?.startsWith("A pull request already exists"),
      )
    );
  } catch {
    return false;
  }
};

async function create(o: GitOptions, input: Input): Promise<ToolRun> {
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
  // A server error may have created it: settled by lookup.
  if (res.status >= 500) return { kind: "unknown", reason: "transport_error" };
  const text = await res.text();
  if (res.status === 422 && alreadyExists(text)) {
    const found = await existing(o, input);
    if (found.status === "found") return done(found.value);
  }
  // Any other answer is the forge refusing: nothing was created.
  return done(
    `the forge refused the pull request (${res.status}): ${text}`,
    true,
  );
}

async function open(o: GitOptions, input: Input): Promise<ToolRun> {
  const found = await existing(o, input);
  if (found.status === "found") return done(found.value);
  // Unsure whether one exists: nothing was sent yet, so nothing is created blind.
  if (found.status === "unknown")
    return done(`the forge lookup failed: ${found.reason}`, true);
  return create(o, input);
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
    // No pull request for (head, base) in any state means none was created: not_found is final.
    reconcile: () => ({
      finality: "final",
      lookup: async (_key, raw) => {
        const input = OpenPullRequestInput.safeParse(raw);
        return input.success
          ? existing(o, input.data)
          : {
              status: "unknown",
              reason: "the call's input is not open_pull_request's",
            };
      },
    }),
  });
}
