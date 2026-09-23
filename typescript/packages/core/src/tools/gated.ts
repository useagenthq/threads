import { ConfigError } from "../agent/errors";
import type { Sandbox } from "../sandbox/protocol";
import type { Builtin } from "./builtin";
import { computer, computerScreenshot } from "./computer";
import { gitClone, gitFetch } from "./git/clone";
import type { GitOptions } from "./git/host";
import { openPullRequest } from "./git/pull-request";
import { gitPush } from "./git/push";
import { lsp } from "./lsp";
import { webFetch } from "./web-fetch";
import { type SearchBackend, webSearch } from "./web-search";
import type { WebTransport } from "./web-transport";

// The capability-gated built-ins: offered only when
// their capability is configured. Naming one whose capability is missing is a setup error that
// names it; nothing is ever offered and then answered with an empty success.

/** spec/api.json agent options web, git, computer and lsp. */
export type Capabilities = {
  readonly web?: {
    readonly fetch?: boolean;
    readonly search?: SearchBackend;
    /** The host network web_fetch uses; tests inject one. */
    readonly transport?: WebTransport;
  };
  readonly git?: GitOptions;
  readonly computer?: boolean;
  readonly lsp?: { readonly languages: readonly string[] };
};

/** Throws ConfigError capability_missing when a configured built-in can't be offered. */
export function requireCapabilities(
  c: Capabilities,
  sandbox: Sandbox | undefined,
): void {
  const missing = (what: string, why: string): never => {
    throw new ConfigError("capability_missing", `${what}: ${why}`);
  };
  if (c.git !== undefined && sandbox === undefined)
    missing("git", "the git gateway clones into a sandbox; configure one");
  if (c.lsp !== undefined && sandbox === undefined)
    missing("lsp", "language servers run in the sandbox; configure one");
  if (c.computer === true && (sandbox?.info.desktop ?? "none") === "none")
    missing(
      "computer",
      `sandbox ${sandbox?.info.provider ?? "(none)"} has no desktop (info.desktop is none)`,
    );
}

/** The gated built-ins this config offers; sandbox ones only with a sandbox. */
export function gated(
  c: Capabilities,
  sandbox: Sandbox | undefined,
): readonly Builtin[] {
  const desktop = (sandbox?.info.desktop ?? "none") !== "none";
  return [
    ...(c.web?.fetch === true ? [webFetch(c.web.transport)] : []),
    ...(c.web?.search === undefined ? [] : [webSearch(c.web.search)]),
    ...(c.git === undefined || sandbox === undefined
      ? []
      : [
          gitClone(c.git),
          gitFetch(c.git),
          gitPush(c.git),
          openPullRequest(c.git),
        ]),
    ...(c.computer === true && desktop ? [computer, computerScreenshot] : []),
    ...(c.lsp === undefined || sandbox === undefined
      ? []
      : [lsp(c.lsp.languages)]),
  ];
}
