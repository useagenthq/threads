import type { ArtifactRef, EventDraft, ToolContext } from "threadsai/adapter";
import type { Remote } from "../a2a";
import { fetchCard, type PinnedCard, pinCard, type Sending } from "../protocol";
import { pinnedCardOf } from "./log";

// The card a thread calls a partner through, resolved for one attempt. The card is pinned at the
// thread's FIRST call of a remote and read back from that pin afterwards, so a card that changes
// never moves a conversation already under way and a re-dispatch in a new process calls the same
// interface with the same declared extensions.

export type Resolved = {
  readonly card: PinnedCard;
  readonly ref: ArtifactRef;
  /** The `remote_card` to append with this attempt's begin, on the thread's first call. */
  readonly draft: EventDraft | undefined;
};

export async function resolveCard(
  remote: Remote,
  ctx: ToolContext,
  sending: Sending,
): Promise<Resolved | { readonly error: string }> {
  const pinned = pinnedCardOf(ctx.events(), remote.name);
  if (pinned !== undefined) return fromPin(remote, ctx, pinned);
  const got = await fetchCard(
    remote.cardUrl,
    remote.auth !== undefined,
    sending,
  );
  if ("code" in got) return { error: `${got.code}: ${got.message}` };
  const ref = await ctx.store(got.bytes, "application/json");
  return {
    card: got,
    ref,
    draft: {
      type: "remote_card",
      type_version: 1,
      critical: true,
      actor: { kind: "host" },
      data: {
        remote: remote.name,
        card_ref: ref,
        interface_url: got.wire.url,
        binding: got.wire.binding,
      },
    },
  };
}

/**
 * The pinned bytes, parsed again. Which interface to call is the event's answer, not this parse's:
 * a card whose stored bytes offer two interfaces must keep being called on the one the thread
 * pinned, whatever a re-run of the choice would prefer today.
 */
async function fromPin(
  remote: Remote,
  ctx: ToolContext,
  pinned: NonNullable<ReturnType<typeof pinnedCardOf>>,
): Promise<Resolved | { readonly error: string }> {
  const bytes = await ctx.read(pinned.cardRef);
  if (!bytes.ok)
    return {
      error: `remote_unavailable: the card ${remote.name} pinned could not be read: ${bytes.error.message}`,
    };
  const card = pinCard(bytes.value, remote.cardUrl, remote.auth !== undefined);
  if ("code" in card) return { error: `${card.code}: ${card.message}` };
  return {
    card: { ...card, wire: pinned.wire },
    ref: pinned.cardRef,
    draft: undefined,
  };
}
