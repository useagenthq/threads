import type { ChannelAdapter, Fetch, Secret } from "@threads/core/adapter";
import { ack, parser, type Tenancy, verifier } from "./inbound";
import { lookuper, performer, render, renderText } from "./outbound";

export type SlackOptions = {
  readonly agent: string;
  readonly signingSecret: Secret;
  readonly botToken: Secret;
  /** The bot's own user id: its messages are ignored. */
  readonly botUserId?: string;
  /** Default `slack:<team_id>`; a function answering undefined refuses that workspace. */
  readonly tenant?: Tenancy;
  readonly now?: () => number;
  /** The transport the SDK sends through (tests); always behind the sandbox fence. */
  readonly fetch?: Fetch;
};

/** The Slack channel adapter (spec/api.json ChannelAdapter). */
export function slack(options: SlackOptions): ChannelAdapter {
  const transport: Fetch =
    options.fetch ?? ((input, init) => fetch(input, init));
  return {
    agent: options.agent,
    capabilities: {
      lookup: "nonfinal",
      buttons: true,
      edits: true,
      files: true,
      direct_messages: true,
      delivery: "reliable",
    },
    limits: { message_bytes: 40_000 },
    secrets: { botToken: options.botToken },
    verify: verifier(
      options.signingSecret,
      options.tenant,
      options.now ?? Date.now,
    ),
    parse: parser(options.tenant, options.botUserId),
    ack,
    render,
    renderText,
    perform: performer(transport),
    lookup: lookuper(transport, () => options.botToken.reveal()),
  };
}
