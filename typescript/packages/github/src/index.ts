import type { ChannelAdapter, Fetch, Secret } from "@threads/core/adapter";
import { parser, type TenantOf, verifier } from "./inbound";
import { looker, performer, render, renderText } from "./outbound";

// github(): GitHub issues and pull requests as a threads channel (spec/api.json ChannelAdapter,
//). Inbound is a GitHub App webhook; outbound is an issue comment through the official
// SDK (@octokit/rest). `token` is an installation (or personal) token: minting one from an App
// private key is out of scope. GitHub has no buttons, so approvals are `/approve <id>` replies.

export type GithubOptions = {
  /** The host agent key this channel routes to. */
  readonly agent: string;
  readonly webhookSecret: Secret;
  readonly token: Secret;
  /** The app's slug: its bot login is `<slug>[bot]`, whose comments are ignored. */
  readonly appSlug?: string;
  /** Tenant for an installation; undefined rejects it. Defaults to `github:<installation_id>`. */
  readonly tenant?: string | TenantOf;
  /** The transport under the fence. Defaults to the process's fetch. */
  readonly fetch?: Fetch;
};

const EMPTY = new Uint8Array();

export function github(options: GithubOptions): ChannelAdapter {
  const { tenant } = options;
  const tenantOf: TenantOf =
    typeof tenant === "function" ? tenant : (id) => tenant ?? `github:${id}`;
  const inner: Fetch = options.fetch ?? ((input, init) => fetch(input, init));
  return {
    agent: options.agent,
    capabilities: {
      lookup: "nonfinal",
      buttons: false,
      edits: true,
      files: false,
      direct_messages: false,
      delivery: "reliable",
    },
    limits: { comment_bytes: 65536 },
    secrets: { token: options.token },
    verify: verifier(options.webhookSecret, tenantOf),
    parse: parser(options.appSlug, tenantOf),
    ack: () => ({ status: 200, headers: {}, body: EMPTY }),
    render,
    renderText,
    perform: performer(inner),
    lookup: looker(options.token, inner, options.appSlug),
  };
}
