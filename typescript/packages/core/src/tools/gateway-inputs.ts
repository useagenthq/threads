import { z } from "zod";
import type { Arr, Opt, Strict } from "../log/zod-types";

// Inputs of the host-side gateway tools: web_fetch and web_search run
// on the host through the egress gateway; the git tools run on the host with the credential,
// which never enters the sandbox (invariant 4).

const Text: z.ZodString = z.string().min(1);
const Domains: Opt<Arr<z.ZodString>> = z
  .array(Text)
  .optional()
  .describe("Hosts; a subdomain of a listed host matches too.");
const Repo: z.ZodString = z
  .string()
  .regex(/^[A-Za-z0-9_.-]+[/][A-Za-z0-9_.-]+$/)
  .describe("owner/name on the configured forge.");
const Clone: Opt<z.ZodString> = z
  .string()
  .min(1)
  .optional()
  .describe(
    "The clone's directory, relative to /workspace; default the repo name.",
  );

export const WebFetchInput: Strict<{ url: z.ZodString }> = z.strictObject({
  url: z
    .string()
    .regex(/^https?:[/][/][!-~]+$/)
    .describe(
      "An http or https URL, printable ASCII (percent-encode the rest).",
    ),
});

export const WebSearchInput: Strict<{
  query: z.ZodString;
  allowed_domains: Opt<Arr<z.ZodString>>;
  blocked_domains: Opt<Arr<z.ZodString>>;
}> = z.strictObject({
  query: Text,
  allowed_domains: Domains,
  blocked_domains: Domains,
});

export const GitCloneInput: Strict<{
  repo: z.ZodString;
  ref: Opt<z.ZodString>;
  path: Opt<z.ZodString>;
}> = z.strictObject({
  repo: Repo,
  ref: Text.optional().describe(
    "Branch, tag or commit; default the default branch.",
  ),
  path: Clone,
});

export const GitFetchInput: Strict<{
  repo: z.ZodString;
  ref: Opt<z.ZodString>;
  path: Opt<z.ZodString>;
}> = z.strictObject({
  repo: Repo,
  ref: Text.optional().describe("Fetch only this ref; default every branch."),
  path: Clone,
});

export const GitPushInput: Strict<{
  repo: z.ZodString;
  branch: z.ZodString;
  path: Opt<z.ZodString>;
}> = z.strictObject({
  repo: Repo,
  branch: Text.describe("The local branch; it is pushed to the same name."),
  path: Clone,
});

export const OpenPullRequestInput: Strict<{
  repo: z.ZodString;
  head: z.ZodString;
  base: z.ZodString;
  title: z.ZodString;
  body: Opt<z.ZodString>;
}> = z.strictObject({
  repo: Repo,
  head: Text.describe("The pushed branch."),
  base: Text.describe("The branch to merge into."),
  title: Text,
  body: z.string().optional(),
});
