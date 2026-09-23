import { Octokit } from "@octokit/rest";
import {
  type ChannelAdapter,
  type DeliveryOutcome,
  FenceRefused,
  type Fetch,
  type LookupResult,
  type Secret,
  sandboxFetch,
} from "@threads/core/adapter";
import { z } from "zod";
import { MARKER_PREFIX } from "./inbound";

// Outbound comments as effects. Octokit sends through `sandboxFetch`, so the
// host's fence runs as each request leaves; @octokit/rest ships no retry or
// throttle plugin, so one call is one transport attempt. A fence refusal is never caught here.

type Render = ChannelAdapter["render"];
type Op = z.infer<typeof Op>;

const Op = z.strictObject({
  kind: z.enum(["comment", "approval"]),
  text: z.string().min(1),
  challenge_id: z.string().optional(),
  address: z.string().regex(/^[^/\s]+\/[^/#\s]+#[1-9][0-9]*$/),
  installation_id: z.string(),
  // Host-added; GitHub has no reply window, so it is unused.
  last_inbound_at: z.number().optional(),
});
const Created = z.object({ id: z.int() });
const Listed = z.array(
  z.object({
    id: z.int(),
    body: z.string().optional(),
    user: z.object({ login: z.string() }).nullable(),
  }),
);

/** Errors a fetch raises when the connection never opened, so no request byte was written. */
const NOT_CONNECTED = new Set([
  "ECONNREFUSED",
  "ENOTFOUND",
  "EAI_AGAIN",
  "ConnectionRefused",
  "FailedToOpenSocket",
]);

const marker = (effectKey: string): string =>
  `${MARKER_PREFIX}${effectKey} -->`;

export const render: Render = (event) => {
  switch (event.type) {
    case "model_response": {
      const text = event.data.content
        .flatMap((p) => (p.type === "text" ? [p.text] : []))
        .join("");
      return text === "" ? [] : [{ kind: "comment", text }];
    }
    case "approval_requested": {
      const id = event.data.challenge_id;
      const text = `Approval needed for call \`${event.data.call_id}\` (args sha256 \`${event.data.args_hash}\`). Reply \`/approve ${id}\` or \`/deny ${id}\`.`;
      return [{ kind: "approval", challenge_id: id, text }];
    }
    default:
      return [];
  }
};

function target(address: string): {
  owner: string;
  repo: string;
  issue_number: number;
} {
  const [repoPath = "", number = ""] = address.split("#");
  const [owner = "", repo = ""] = repoPath.split("/");
  return { owner, repo, issue_number: Number(number) };
}

const quiet = (): void => {};

function client(token: string, inner: Fetch): Octokit {
  return new Octokit({
    auth: token,
    userAgent: "threads-github",
    request: { fetch: sandboxFetch(inner) },
    // Failures come back as DeliveryOutcome values; Octokit's per-request error log is noise.
    log: { debug: quiet, info: quiet, warn: console.warn, error: quiet },
  });
}

/** Rethrows a fence refusal however Octokit wrapped it; otherwise the error's code chain. */
function codes(error: unknown): readonly string[] {
  const out: string[] = [];
  for (let e = error; e instanceof Error; e = e.cause) {
    if (e instanceof FenceRefused) throw e;
    if ("code" in e && typeof e.code === "string") out.push(e.code);
  }
  return out;
}

const failed = (
  kind: "rate_limited" | "transient" | "permanent",
  sent: "definite_not_sent" | "outcome_unknown",
): DeliveryOutcome => ({ status: "delivery_error", kind, sent });

function response(
  error: unknown,
): { status: number; headers: Readonly<Record<string, unknown>> } | undefined {
  if (!(error instanceof Error) || !("response" in error)) return undefined;
  const r = z
    .object({ status: z.int(), headers: z.record(z.string(), z.unknown()) })
    .safeParse(error.response);
  return r.success ? r.data : undefined;
}

function classify(error: unknown): DeliveryOutcome {
  const chain = codes(error);
  const answered = response(error);
  if (answered === undefined)
    return chain.some((c) => NOT_CONNECTED.has(c))
      ? failed("transient", "definite_not_sent")
      : failed("transient", "outcome_unknown");
  const { status, headers } = answered;
  const limited =
    status === 429 ||
    (status === 403 &&
      (headers["retry-after"] !== undefined ||
        headers["x-ratelimit-remaining"] === "0"));
  if (limited) return failed("rate_limited", "definite_not_sent");
  if (status >= 400 && status < 500)
    return failed("permanent", "definite_not_sent");
  return failed("transient", "outcome_unknown");
}

export function performer(inner: Fetch): ChannelAdapter["perform"] {
  return async (raw, effectKey, credentials) => {
    const op = Op.safeParse(raw);
    const token = credentials["token"];
    if (!op.success || token === undefined)
      return failed("permanent", "definite_not_sent");
    const to = target(op.data.address);
    try {
      const res = await client(token, inner).rest.issues.createComment({
        ...to,
        body: `${op.data.text}\n\n${marker(effectKey)}`,
      });
      const made = Created.safeParse(res.data);
      // Created but unreadable: lookup finds it by its marker.
      if (!made.success) return failed("transient", "outcome_unknown");
      return {
        status: "sent",
        platform_ref: `${op.data.address}:comment:${made.data.id}`,
      };
    } catch (error) {
      return classify(error);
    }
  };
}

async function find(
  octokit: Octokit,
  op: Op,
  effectKey: string,
  author: string | undefined,
): Promise<string | undefined> {
  const want = marker(effectKey);
  // ponytail: walks every page oldest first; fine for issue-sized threads.
  const pages = octokit.paginate.iterator(octokit.rest.issues.listComments, {
    ...target(op.address),
    per_page: 100,
  });
  for await (const page of pages) {
    const hit = Listed.parse(page.data).find(
      (c) =>
        c.body?.includes(want) === true &&
        (author === undefined || c.user?.login === author),
    );
    if (hit !== undefined) return `${op.address}:comment:${hit.id}`;
  }
  return undefined;
}

/**
 * Nonfinal: a create that timed out may still land after the listing, and GitHub's reads can lag
 * its writes, so a marker's absence never proves the comment was not posted.
 */
export function looker(
  token: Secret,
  inner: Fetch,
  appSlug: string | undefined,
): ChannelAdapter["lookup"] {
  return async (effectKey, raw): Promise<LookupResult<string>> => {
    const op = Op.safeParse(raw);
    if (!op.success)
      return { status: "unknown", reason: "github: op does not parse" };
    const author = appSlug === undefined ? undefined : `${appSlug}[bot]`;
    try {
      const ref = await find(
        client(token.reveal(), inner),
        op.data,
        effectKey,
        author,
      );
      return ref === undefined
        ? { status: "not_found_nonfinal" }
        : { status: "found", value: ref };
    } catch (error) {
      codes(error);
      const status = response(error)?.status;
      return {
        status: "unknown",
        reason: `github: listing comments failed${status === undefined ? "" : ` (HTTP ${status})`}`,
      };
    }
  };
}
