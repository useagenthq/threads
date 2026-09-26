import {
  canonicalize,
  cursorAgainst,
  err,
  feedHead,
  Int,
  ok,
  type Principal,
  parseRows,
  type Result,
  reading,
  type TeamCursor,
  TeamId,
  type TeamItem,
  teamEvents,
} from "@threads/core/host";
import { z } from "zod";
import type { HostContext } from "./context";
import { type Failure, failure } from "./errors";

// GET /v1/teams/{team}/events (spec/schema/host-api/openapi.json subscribeTeam): a lead team's
// feed as server-sent events. A pure read of team_feed: it never writes and never drives the
// team. The route always follows, and closes on epoch_restarted so the client reconnects with
// the new cursor.

/** A host team has no HTTP stream (29B decision 2): only Host.team reaches it. */
const TeamsRow = z.strictObject({
  tenant_id: z.string(),
  kind: z.enum(["lead", "host"]),
});

/** `: keepalive` every 15 s keeps proxies open; it moves no cursor. Tests inject less. */
export const KEEPALIVE_MS = 15_000;

export async function subscribeTeam(
  ctx: HostContext,
  team: TeamId,
  principal: Principal,
  after: TeamCursor | undefined,
): Promise<Result<AsyncIterable<TeamItem>, Failure>> {
  const { log } = await ctx.open(principal.tenant);
  const rows = parseRows(
    TeamsRow,
    await reading(log.driver, (tx) =>
      tx.all("SELECT tenant_id, kind FROM teams WHERE team_id = ?", [team]),
    ),
  );
  if (!rows.ok) return err({ code: "not_found", message: `no team ${team}` });
  const row = rows.value[0];
  // Another tenant's team, or the tenant's host team, is not found here.
  if (
    row === undefined ||
    row.tenant_id !== principal.tenant ||
    row.kind !== "lead"
  )
    return err({
      code: "not_found",
      message: `no lead team ${team} in ${principal.tenant}`,
    });
  const head = await feedHead(log, team);
  if (head !== undefined && after !== undefined) {
    const against = cursorAgainst(head, after);
    if (against === "invalid_cursor")
      return err({
        code: "invalid_cursor",
        message: `cursor ${after.epoch}:${after.offset} is not in this feed`,
      });
  }
  return ok(
    teamEvents(log, team, {
      follow: true,
      ...(after === undefined ? {} : { after }),
    }),
  );
}

/** Each half of a cursor: digits only, so "1:" and " 1:2" are refused as Python refuses them. */
const DIGITS = /^-?\d+$/;

/** `Last-Event-ID` wins over `?after` (lane 25's split, fixed here), in both hosts. */
export function teamCursor(
  header: string | null,
  query: string | null,
): Result<TeamCursor | undefined, Failure> {
  const raw = header ?? query;
  if (raw === null || raw === "") return ok(undefined);
  const [epoch, offset, ...rest] = raw.split(":");
  const shaped =
    rest.length === 0 &&
    epoch !== undefined &&
    offset !== undefined &&
    DIGITS.test(epoch) &&
    DIGITS.test(offset);
  const parsed = shaped
    ? z.tuple([Int, Int]).safeParse([Number(epoch), Number(offset)])
    : undefined;
  if (parsed === undefined || !parsed.success)
    return err({
      code: "invalid_cursor",
      message: `${raw} is not <epoch>:<offset>`,
    });
  return ok({ epoch: parsed.data[0], offset: parsed.data[1] });
}

export async function teamEventsRoute(
  ctx: HostContext,
  principal: Principal,
  teamParam: string | undefined,
  request: Request,
): Promise<Response> {
  const team = TeamId.safeParse(teamParam);
  if (!team.success) return failure("not_found", `no team ${teamParam}`);
  const url = new URL(request.url);
  const after = teamCursor(
    request.headers.get("last-event-id"),
    url.searchParams.get("after"),
  );
  if (!after.ok) return failure(after.error.code, after.error.message);
  const stream = await subscribeTeam(ctx, team.data, principal, after.value);
  if (!stream.ok) return failure(stream.error.code, stream.error.message);
  return new Response(teamSse(stream.value), {
    status: 200,
    headers: {
      "content-type": "text/event-stream",
      "cache-control": "no-cache",
    },
  });
}

/** One SSE message per item: `id: <epoch>:<offset>` and the item as RFC 8785 JSON. */
function frame(item: TeamItem): string {
  const body = canonicalize(item);
  if (!body.ok) throw new Error("a team item is JSON");
  return `id: ${item.cursor.epoch}:${item.cursor.offset}\ndata: ${body.value}\n\n`;
}

export function teamSse(
  items: AsyncIterable<TeamItem>,
  keepaliveMs: number = KEEPALIVE_MS,
): ReadableStream<Uint8Array> {
  const utf8 = new TextEncoder();
  const iterator = items[Symbol.asyncIterator]();
  let pending: Promise<IteratorResult<TeamItem>> | undefined;
  return new ReadableStream({
    async pull(controller) {
      pending ??= iterator.next();
      const beat = Promise.withResolvers<"keepalive">();
      const timer = setTimeout(() => beat.resolve("keepalive"), keepaliveMs);
      const won = await Promise.race([pending, beat.promise]);
      clearTimeout(timer);
      if (won === "keepalive") {
        controller.enqueue(utf8.encode(": keepalive\n\n"));
        return;
      }
      pending = undefined;
      if (won.done === true) {
        controller.close();
        return;
      }
      controller.enqueue(utf8.encode(frame(won.value)));
      // The client reconnects with the new cursor (lane 25 Part B's rule).
      if (won.value.kind === "epoch_restarted") {
        await iterator.return?.();
        controller.close();
      }
    },
    async cancel() {
      await iterator.return?.();
    },
  });
}
