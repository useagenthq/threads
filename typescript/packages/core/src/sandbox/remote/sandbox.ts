import { err, ok, type Result } from "../../result";
import type {
  Failure,
  ReleaseFailure,
  RestoreFailure,
  Sandbox,
  SandboxInfo,
  SandboxSession,
} from "../protocol";
import type { ProviderExpiry, ProviderSandbox, SandboxDriver } from "./driver";
import { guarded, messageOf, refusedHere, within } from "./fence";
import { INIT_SCRIPT } from "./scripts";
import { remoteSession, runScript, treeHash } from "./session";

// remoteSandbox(): a provider driver as the Sandbox protocol (spec/api.json). Creation is
// findable by its operation key, and a restore verifies the restored tree against the snapshot
// event's manifest hash before anyone uses it.

export type RemoteInfo = Omit<
  SandboxInfo,
  "lookup" | "termination" | "capture_classes"
>;

export function remoteSandbox(
  driver: SandboxDriver,
  declared: RemoteInfo,
  expiry: ProviderExpiry,
): ProviderSandbox {
  const quiescence = driver.snapshot?.quiescence ?? "none";
  const info: SandboxInfo = {
    ...declared,
    // A lookup by tag or name can find a create, but a create still in flight at the provider
    // may appear later, so absence is never proof. There's no snapshot lookup: its event data
    // (manifest hash, quiescence) can't be recovered from the provider.
    lookup: { create: "nonfinal", snapshot: "none" },
    // A process kill happens inside the guest, so it never proves a whole group is gone.
    termination: "unconfirmed",
    capture_classes:
      quiescence === "none" || quiescence === "unconfirmed"
        ? []
        : ["filesystem"],
  };
  const session = (id: string) => remoteSession(driver, info.provider, id);
  const unavailable = (error: unknown) =>
    ({ code: "unavailable", message: messageOf(error) }) as const;

  const create: Sandbox["create"] = (operationKey, context) =>
    guarded<SandboxSession, Failure<"unavailable" | "timeout">>(
      context,
      async () => {
        const made = await driver.create(operationKey, undefined);
        if (made.kind !== "created")
          return err({ code: "unavailable", message: made.message });
        const init = await runScript(driver, made.id, INIT_SCRIPT);
        return init.exit === 0
          ? ok(session(made.id))
          : err({
              code: "unavailable",
              message: "the workspace can't be made",
            });
      },
      unavailable,
    );

  /**
   * A tree that fails the hash is released before the error returns. Once the sandbox exists,
   * no failure is `unavailable` (that would read as a lost create and use the sandbox
   * unverified): it is snapshot_restore_failed, and the ledger finds and releases it.
   */
  const verify = async (
    id: string,
    expected: string,
  ): Promise<Result<SandboxSession, RestoreFailure>> => {
    try {
      const hash = await treeHash(driver, id);
      if (hash === expected) return ok(session(id));
      if (hash !== undefined) {
        await driver.kill(id);
        return err({
          code: "snapshot_manifest_mismatch",
          message: `the restored tree of ${id} fails manifest ${expected}`,
        } as const);
      }
      return err({
        code: "snapshot_restore_failed",
        message: `the restored tree of ${id} can't be read`,
      } as const);
    } catch (error) {
      if (refusedHere()) throw error;
      return err({
        code: "snapshot_restore_failed",
        message: messageOf(error),
      } as const);
    }
  };

  const restore: Sandbox["restore"] = (
    snapshotId,
    expected,
    operationKey,
    context,
  ) =>
    guarded<SandboxSession, RestoreFailure>(
      context,
      async () => {
        if (driver.snapshot === undefined)
          return err({
            code: "snapshot_restore_failed",
            message: `${info.provider} has no snapshots`,
          });
        // A throw here is a lost create response: the ledger resolves it by lookup.
        const made = await driver.create(operationKey, snapshotId);
        if (made.kind !== "created")
          return err({ code: made.kind, message: made.message });
        return verify(made.id, expected);
      },
      unavailable,
    );

  return {
    info,
    expiry,
    quiescence,
    create,
    restore,
    lookup: async (operationKey, context) => {
      const found = await within(context, () => driver.find(operationKey));
      if (!found.ok)
        return found.error.stale === undefined
          ? ok({ status: "unknown", reason: messageOf(found.error.error) })
          : err(found.error.stale);
      const answer = found.value;
      return ok(
        answer.status === "found"
          ? { status: "found", value: session(answer.value) }
          : answer,
      );
    },
    attach: (ref, context) =>
      guarded<
        SandboxSession,
        Failure<"not_found" | "resource_unknown" | "unavailable">
      >(
        context,
        async () =>
          (await driver.exists(ref))
            ? ok(session(ref))
            : err({ code: "not_found", message: `no live sandbox ${ref}` }),
        unavailable,
      ),
    release: (ref, context) =>
      guarded<"released" | "already_gone", ReleaseFailure>(
        context,
        async () => ok(await driver.deleteSnapshot(ref)),
        (error) =>
          ({ code: "release_failed", message: messageOf(error) }) as const,
      ),
  };
}
