import type { Inbound } from "@threads/core/adapter";
import { z } from "zod";

// The Cloud API webhook (a trust boundary). Objects are loose because Meta adds fields over
// time; only what threads reads is required. A message of a type threads doesn't read (image,
// sticker, location...) still parses, and becomes an ignore item.

const Id = z.string().min(1);

const Message = z.looseObject({
  from: Id,
  id: Id,
  type: z.string(),
  text: z.looseObject({ body: z.string() }).optional(),
  interactive: z
    .looseObject({
      button_reply: z.looseObject({ id: z.string() }).optional(),
    })
    .optional(),
});
type Message = z.infer<typeof Message>;

const Change = z.looseObject({
  value: z.looseObject({
    metadata: z.looseObject({ phone_number_id: Id }),
    messages: z.array(Message).optional(),
    statuses: z.array(z.unknown()).optional(),
  }),
});

const Webhook = z.looseObject({
  object: z.literal("whatsapp_business_account"),
  entry: z.array(z.looseObject({ changes: z.array(Change) })).min(1),
});
type Webhook = z.infer<typeof Webhook>;

function readWebhook(body: Uint8Array): Webhook | undefined {
  let json: unknown;
  try {
    json = JSON.parse(new TextDecoder().decode(body));
  } catch {
    return undefined;
  }
  return Webhook.safeParse(json).data;
}

/** Every business phone number (installation) the webhook speaks for; none if malformed. */
export function phoneNumbersOf(body: Uint8Array): readonly string[] {
  const changes = readWebhook(body)?.entry.flatMap((e) => e.changes) ?? [];
  return [...new Set(changes.map((c) => c.value.metadata.phone_number_id))];
}

/** Button ids carry only the challenge id and the decision. */
const ButtonDecision = z.strictObject({
  challenge_id: z.uuid(),
  decision: z.enum(["grant", "deny"]),
});

function decisionOf(id: string): z.infer<typeof ButtonDecision> | undefined {
  const [verb, challenge] = id.split(":", 2);
  if (verb === "approve" || verb === "deny")
    return ButtonDecision.safeParse({
      challenge_id: challenge,
      decision: verb === "approve" ? "grant" : "deny",
    }).data;
  try {
    return ButtonDecision.safeParse(JSON.parse(id)).data;
  } catch {
    return undefined;
  }
}

function itemOf(message: Message, issuer: string, tenant: string): Inbound {
  const base = {
    principal: { issuer, tenant, subject: message.from },
    address: message.from,
    item_key: message.id,
  };
  const body = message.text?.body;
  if (message.type === "text" && body !== undefined && body !== "")
    return { kind: "message", ...base, content: body };
  const button = message.interactive?.button_reply?.id;
  const decision =
    message.type === "interactive" && button !== undefined
      ? decisionOf(button)
      : undefined;
  return decision === undefined
    ? { kind: "ignore" }
    : { kind: "decision", ...base, ...decision };
}

/**
 * One item per message, keyed by its own `messages[].id`. Delivery status
 * webhooks are ignored for now; they should become events.
 * Undefined when the body is malformed or a phone number has no tenant.
 */
export function itemsOf(
  body: Uint8Array,
  tenantOf: (phoneNumberId: string) => string | undefined,
): readonly Inbound[] | undefined {
  const webhook = readWebhook(body);
  if (webhook === undefined) return undefined;
  const items: Inbound[] = [];
  for (const { value } of webhook.entry.flatMap((e) => e.changes)) {
    const phone = value.metadata.phone_number_id;
    const tenant = tenantOf(phone);
    if (tenant === undefined) return undefined;
    for (const message of value.messages ?? [])
      items.push(itemOf(message, `whatsapp:${phone}`, tenant));
    for (const _ of value.statuses ?? []) items.push({ kind: "ignore" });
  }
  return items;
}
