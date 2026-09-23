import { createHmac, timingSafeEqual } from "node:crypto";
import type {
  ChannelAdapter,
  Inbound,
  RawRequest,
  Secret,
} from "@threads/core/adapter";
import { z } from "zod";

// Webhook intake. GitHub signs the raw body
// with X-Hub-Signature-256 and sends no timestamp, so replay protection is the host inbox's
// dedup on X-GitHub-Delivery. The installation id is the identity authority: it names the
// issuer and, by default, the tenant. The repository is in the address, not the installation id,
// because the inbox key is already unique per delivery and repo-level approver policy is the host's.

export const MARKER_PREFIX = "<!-- threads:effect_key=";

export type TenantOf = (installationId: string) => string | undefined;

type Verify = ChannelAdapter["verify"];
type Parse = ChannelAdapter["parse"];

const Installed = z.object({ installation: z.object({ id: z.int() }) });
const Common = {
  action: z.string(),
  installation: z.object({ id: z.int() }),
  repository: z.object({ full_name: z.string() }),
  sender: z.object({ id: z.int(), login: z.string(), type: z.string() }),
};
const Comment = z.object({
  ...Common,
  comment: z.object({ body: z.string() }),
  issue: z.object({ number: z.int() }),
});
const ReviewComment = z.object({
  ...Common,
  comment: z.object({ body: z.string() }),
  pull_request: z.object({ number: z.int() }),
});
const Opened = z.object({
  ...Common,
  issue: z.object({
    number: z.int(),
    title: z.string(),
    body: z.string().nullable(),
  }),
});

type Item = {
  readonly common: z.infer<z.ZodObject<typeof Common>>;
  readonly number: number;
  readonly text: string;
};
type Picked =
  | { readonly ok: true; readonly item: Item | undefined }
  | { readonly ok: false; readonly message: string };

const DECISION =
  /^\/(approve|deny) ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/;
const IGNORE: Inbound = { kind: "ignore" };

function decode(body: Uint8Array): unknown {
  try {
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body));
  } catch {
    return undefined;
  }
}

function signed(raw: RawRequest, key: string): boolean {
  const header = raw.headers["x-hub-signature-256"] ?? "";
  if (!/^sha256=[0-9a-f]{64}$/.test(header)) return false;
  const want = createHmac("sha256", key).update(raw.body).digest();
  return timingSafeEqual(Buffer.from(header.slice(7), "hex"), want);
}

export function verifier(webhookSecret: Secret, tenantOf: TenantOf): Verify {
  return (raw) => {
    const unverified = (message: string) =>
      ({ ok: false, error: { code: "unverified", message } }) as const;
    if (!signed(raw, webhookSecret.reveal()))
      return unverified("github: bad or missing X-Hub-Signature-256");
    const deliveryId = raw.headers["x-github-delivery"];
    if (deliveryId === undefined || deliveryId === "")
      return unverified("github: missing X-GitHub-Delivery");
    const body = Installed.safeParse(decode(raw.body));
    if (!body.success) return unverified("github: payload has no installation");
    const installationId = String(body.data.installation.id);
    const tenant = tenantOf(installationId);
    if (tenant === undefined)
      return unverified(
        `github: installation ${installationId} is not configured`,
      );
    return {
      ok: true,
      value: {
        tenant,
        installation_id: installationId,
        delivery_id: deliveryId,
      },
    };
  };
}

function pick<T extends { readonly action: string }>(
  schema: z.ZodType<T>,
  json: unknown,
  action: string,
  to: (data: T) => Item,
): Picked {
  const p = schema.safeParse(json);
  if (!p.success) return { ok: false, message: p.error.message };
  return { ok: true, item: p.data.action === action ? to(p.data) : undefined };
}

function opened(data: z.infer<typeof Opened>): Item {
  const { title, body, number } = data.issue;
  const text = body === null || body === "" ? title : `${title}\n\n${body}`;
  return { common: data, number, text };
}

/** The item a supported event carries; undefined for events that are ignored. */
function itemOf(event: string | undefined, json: unknown): Picked {
  switch (event) {
    case "issue_comment":
      return pick(Comment, json, "created", (d) => ({
        common: d,
        number: d.issue.number,
        text: d.comment.body,
      }));
    case "pull_request_review_comment":
      return pick(ReviewComment, json, "created", (d) => ({
        common: d,
        number: d.pull_request.number,
        text: d.comment.body,
      }));
    case "issues":
      return pick(Opened, json, "opened", opened);
    default:
      return { ok: true, item: undefined };
  }
}

/** Bots (the app itself included) and threads' own marked comments never start a turn. */
function skipped(item: Item, appSlug: string | undefined): boolean {
  const { sender } = item.common;
  const own = sender.type === "Bot" || sender.login === `${appSlug}[bot]`;
  return own || item.text.trim() === "" || item.text.includes(MARKER_PREFIX);
}

function inbound(item: Item, tenant: string, deliveryId: string): Inbound {
  const { sender, installation, repository } = item.common;
  const base = {
    principal: {
      issuer: `github:${installation.id}`,
      tenant,
      subject: String(sender.id),
    },
    address: `${repository.full_name}#${item.number}`,
    item_key: `${deliveryId}#0`,
  };
  // GitHub has no buttons: the text fallback is bound to a challenge id.
  const decision = DECISION.exec(item.text.trim());
  if (decision?.[2] === undefined)
    return { kind: "message", ...base, content: item.text };
  return {
    kind: "decision",
    ...base,
    challenge_id: decision[2],
    decision: decision[1] === "approve" ? "grant" : "deny",
  };
}

export function parser(appSlug: string | undefined, tenantOf: TenantOf): Parse {
  return (raw) => {
    const invalid = (message: string) =>
      ({ ok: false, error: { code: "invalid", message } }) as const;
    const json = decode(raw.body);
    if (json === undefined) return invalid("github: body is not JSON");
    const picked = itemOf(raw.headers["x-github-event"], json);
    if (!picked.ok) return invalid(`github: ${picked.message}`);
    const { item } = picked;
    if (item === undefined || skipped(item, appSlug))
      return { ok: true, value: [IGNORE] };
    const installationId = String(item.common.installation.id);
    const tenant = tenantOf(installationId);
    if (tenant === undefined)
      return invalid(
        `github: installation ${installationId} is not configured`,
      );
    const deliveryId = raw.headers["x-github-delivery"] ?? "";
    return { ok: true, value: [inbound(item, tenant, deliveryId)] };
  };
}
