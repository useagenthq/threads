import { z } from "zod";
import { Int, NonEmpty, Sha256 } from "./primitives";
import type { Arr, Lit, Strict } from "./zod-types";

// policy.workspace (lane 16 E): the agent's workspace inputs, resolved once on the host into one
// tree artifact. Every sandbox the thread creates starts from that tree.

const Skipped: Arr<typeof NonEmpty> = z
  .array(NonEmpty)
  .describe(
    "The excluded paths (.git, gitignored, deny-listed), top-most only, sorted by UTF-16 code units; never their contents.",
  );

export const WorkspaceSource: z.ZodDiscriminatedUnion<
  [
    Strict<{ kind: Lit<"files"> }>,
    Strict<{
      kind: Lit<"local_dir">;
      path: typeof NonEmpty;
      skipped: typeof Skipped;
    }>,
    Strict<{
      kind: Lit<"git">;
      repo: typeof NonEmpty;
      ref: typeof NonEmpty;
      commit: typeof NonEmpty;
      skipped: typeof Skipped;
    }>,
  ],
  "kind"
> = z
  .discriminatedUnion("kind", [
    z.strictObject({ kind: z.literal("files") }),
    z.strictObject({
      kind: z.literal("local_dir"),
      path: NonEmpty.describe(
        "The host directory exactly as the agent config wrote it.",
      ),
      skipped: Skipped,
    }),
    z.strictObject({
      kind: z.literal("git"),
      repo: NonEmpty.describe("owner/name on the configured forge."),
      ref: NonEmpty,
      commit: NonEmpty.describe("The commit the ref resolved to."),
      skipped: Skipped,
    }),
  ])
  .meta({ id: "WorkspaceSource" });

export const WorkspacePin: Strict<{
  tree: Strict<{ sha256: typeof Sha256; bytes: typeof Int }>;
  manifest_hash: typeof Sha256;
  sources: Arr<typeof WorkspaceSource>;
}> = z
  .strictObject({
    tree: z
      .strictObject({ sha256: Sha256, bytes: Int })
      .describe("The tree artifact (tree.v1.schema.json)."),
    manifest_hash: Sha256,
    sources: z
      .array(WorkspaceSource)
      .describe("In the order files, local_dir, git; each at most once."),
  })
  .meta({
    id: "WorkspacePin",
    description:
      "The workspace inputs, resolved into one tree on the host before thread_started (spec/schema/README.md, Workspace inputs).",
  });
export type WorkspacePin = z.infer<typeof WorkspacePin>;
