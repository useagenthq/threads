import { describe, expect, test } from "bun:test";
import { forkCases } from "../../core/test/conformance/fork";
import { CTX } from "../../core/test/sandbox/context";
import { remoteHarness } from "../../core/test/sandbox/harness";
import { ledgerSuite } from "../../core/test/sandbox/ledger-suite";
import { CANARY, contractSuite } from "../../core/test/sandbox/remote/contract";
import { losesAfter } from "../../core/test/sandbox/remote/kit";
import { World } from "../../core/test/sandbox/remote/world";
import { code } from "../../core/test/store/helpers";
import { e2b } from "../src";
import { fenceable } from "../src/transport";
import { e2bBackend } from "./backend";

// e2b() over its mocked wire: the shared adapter contract, the fork cases and the ledger
// crash/takeover suite, then what is E2B's own: the fence at the SDK's real transport, and
// its declarations. No network: the SDK's requests reach the mocked backend only.

const DOMAIN = "e2b.test";

function adapter(world: World, internet = false) {
  const backend = e2bBackend(world, DOMAIN);
  const sandbox = e2b({
    apiKey: CANARY,
    domain: DOMAIN,
    internet,
    fetch: backend.fetch,
  });
  return { sandbox, backend };
}

contractSuite("e2b", () => {
  const world = new World();
  const { sandbox, backend } = adapter(world);
  return { sandbox, world, sandboxTraffic: backend.traffic };
});
forkCases(remoteHarness("e2b", (world) => adapter(world).sandbox));
ledgerSuite(remoteHarness("e2b", (world) => adapter(world).sandbox));

describe("e2b transport", () => {
  test("every SDK request is fenced: a lease lost after the create sends nothing more", async () => {
    const world = new World();
    const { sandbox, backend } = adapter(world);
    expect(code(await sandbox.create("op", losesAfter(1)))).toBe("stale_epoch");
    expect(world.creates).toBe(1);
    // The create reached E2B; the workspace script after it never left.
    expect(backend.traffic()).not.toContain("process.Process/Start");
  });

  test("other fetches in the process pass through untouched", async () => {
    const world = new World();
    await adapter(world).sandbox.create("op", CTX);
    expect(await (await fetch("data:text/plain,hi")).text()).toBe("hi");
  });

  test("only runtimes where the SDK sends through fetch are fenceable", () => {
    expect(fenceable()).toBe(true);
    expect(fenceable({})).toBe(false);
  });
});

describe("e2b declarations", () => {
  test("egress, termination, snapshots, lookup and expiry are declared as E2B documents them", () => {
    const { sandbox } = adapter(new World());
    expect(sandbox.info).toEqual({
      provider: "e2b",
      egress: "enforced",
      capture_classes: [],
      browser: "none",
      desktop: "none",
      lookup: { create: "nonfinal", snapshot: "none" },
      termination: "unconfirmed",
    });
    expect(sandbox.quiescence).toBe("unconfirmed");
    expect(sandbox.expiry).toEqual({ sandboxMs: 300_000, snapshotMs: null });
    expect(adapter(new World(), true).sandbox.info.egress).toBe("unenforced");
  });

  test("a sandbox is created with no env, internet off, and tagged by its operation key", async () => {
    const world = new World();
    const { sandbox, backend } = adapter(world);
    await sandbox.create("op-7", CTX);
    const body = backend
      .traffic()
      .split("\n")
      .find((line) =>
        line.startsWith("POST https://api.e2b.test/v2/sandboxes"),
      );
    expect(body).toContain('"envVars":{}');
    expect(body).toContain('"allow_internet_access":false');
    expect(body).toContain('"threads_operation_key":"op-7"');
  });
});
