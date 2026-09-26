import type { KnownEvent } from "../log";
import { parseStrictJson } from "../log/json";
import { recordedStubs } from "../loop/stubs";
import type { StubGateway } from "../loop/types";
import { err, ok, type Result } from "../result";
import type { Sandbox } from "../sandbox/protocol";
import type { ArtifactStore } from "../store";
import { type Egress, SANDBOX_TOOLS } from "../tools";
import { type LogError, logError } from "../verify/error";

// A stub fork's frozen script (ADR 0010, spec/schema/README.md): every run of a branch whose
// resolved chain holds a stub fork answers each mediated operation from the artifact that fork
// recorded, so a reopened child stays stubbed in a fresh process and later parent appends never
// change it.

/** The frozen script of the innermost stub fork on the chain, if the chain has one. */
export function stubForkRef(
  chain: readonly KnownEvent[],
): { readonly sha256: string; readonly bytes: number } | undefined {
  let found: { readonly sha256: string; readonly bytes: number } | undefined;
  for (const event of chain)
    if (event.type === "fork" && event.data.stub_script_ref !== undefined)
      found = event.data.stub_script_ref;
  return found;
}

/**
 * The gateway a stub branch runs behind. Sandbox-local reads stay live when the sandbox enforces
 * deny-all egress, exactly as a live eval's stub run does; everything mediated comes from the
 * script. A chain with no stub fork gets no gateway and runs live.
 */
export async function frozenStubs(
  chain: readonly KnownEvent[],
  artifacts: ArtifactStore,
  sandbox: Sandbox | undefined,
  egress: Egress | undefined,
): Promise<Result<StubGateway | undefined, LogError>> {
  const ref = stubForkRef(chain);
  if (ref === undefined) return ok(undefined);
  const bytes = await artifacts.get(ref.sha256);
  if (!bytes.ok) return bytes;
  if (bytes.value.length !== ref.bytes)
    return err(
      logError(
        "artifact_corrupt",
        `the frozen stub script ${ref.sha256} is ${bytes.value.length} bytes, not ${ref.bytes}`,
      ),
    );
  const json = parseStrictJson(new TextDecoder().decode(bytes.value));
  if (!json.ok)
    return err(
      logError(
        "artifact_corrupt",
        `the frozen stub script ${ref.sha256} is not JSON`,
      ),
    );
  const gateway = recordedStubs(json.value);
  const live = sandbox?.info.egress === "enforced" && egress !== "unenforced";
  return ok(live ? { ...gateway, live: SANDBOX_TOOLS } : gateway);
}
