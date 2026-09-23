import { fakeSandbox, type Sandbox, SandboxScript } from "../../src/sandbox";
import { World } from "./remote/world";

// A sandbox adapter under test with its backend seeded from a conformance SandboxScript. The
// fork cases and the ledger crash/takeover suites run against every adapter through this: the
// fake, and each provider adapter over its mocked transport.

export type Harnessed = {
  readonly sandbox: Sandbox;
  /** Provider create and restore calls that reached the backend. */
  readonly creates: () => number;
};

export type SandboxHarness = {
  readonly name: string;
  readonly make: (script: unknown) => Harnessed;
};

export const fakeHarness: SandboxHarness = {
  name: "fake",
  make: (script) => {
    const sandbox = fakeSandbox(SandboxScript.parse(script ?? {}));
    return { sandbox, creates: sandbox.creates };
  },
};

/**
 * A provider adapter over a World. The corpus records its snapshots as provider `fake`, so the
 * adapter answers to that name here; nothing else about it changes.
 */
export function remoteHarness(
  name: string,
  adapter: (world: World) => Sandbox,
): SandboxHarness {
  return {
    name,
    make: (script) => {
      const world = new World(script ?? {});
      const sandbox = adapter(world);
      return {
        sandbox: { ...sandbox, info: { ...sandbox.info, provider: "fake" } },
        creates: () => world.creates,
      };
    },
  };
}
