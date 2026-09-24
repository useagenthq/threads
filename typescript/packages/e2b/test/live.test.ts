import { describe, expect, test } from "bun:test";
import { within } from "@threads/core/adapter";
import { CTX } from "../../core/test/sandbox/context";
import { run } from "../../core/test/sandbox/remote/kit";
import { code, unwrap } from "../../core/test/store/helpers";
import { e2b } from "../src";
import { e2bDriver } from "../src/driver";

// Live gate: real E2B, so it runs only with THREADS_LIVE=1 and E2B_API_KEY.

const key = process.env["E2B_API_KEY"];
const live = process.env["THREADS_LIVE"] === "1" && key !== undefined;
const utf8 = new TextEncoder();

describe.skipIf(!live)("live gate: e2b", () => {
  test("create, exec, files, terminate and close against real E2B", async () => {
    const sandbox = e2b({ ...(key === undefined ? {} : { apiKey: key }) });
    const box = unwrap(await sandbox.create(crypto.randomUUID(), CTX));
    try {
      const out = await run(
        box,
        ["sh", "-c", "echo out; echo err >&2; exit 3"],
        {
          env: { PATH: "/usr/bin:/bin" },
        },
      );
      expect(out).toEqual({ exit: 3, stdout: "out\n", stderr: "err\n" });
      const env = await run(box, ["/usr/bin/env"], { env: { ONLY: "this" } });
      expect(env.stdout).toBe("ONLY=this\n");
      const bytes = new Uint8Array([0, 1, 2, 255]);
      unwrap(await box.upload("a/b.bin", bytes, CTX));
      expect(unwrap(await box.download("a/b.bin", CTX))).toEqual(bytes);
      expect(code(await box.download("missing", CTX))).toBe("not_found");
      expect(unwrap(await box.terminate("nothing", CTX))).toBe("unknown");
    } finally {
      unwrap(await box.close(CTX));
    }
    expect(code(await sandbox.attach(box.id, CTX))).toBe("not_found");
  });

  test("a restore from an E2B snapshot is verified against the manifest", async () => {
    const sandbox = e2b({ ...(key === undefined ? {} : { apiKey: key }) });
    const box = unwrap(await sandbox.create(crypto.randomUUID(), CTX));
    const driver = e2bDriver({
      apiKey: () => key ?? "",
      domain: "e2b.app",
      template: "base",
      timeoutMs: 300_000,
      internet: false,
      fetch: (input, init) => fetch(input, init),
    });
    try {
      unwrap(await box.upload("f.txt", utf8.encode("one"), CTX));
      const made = await within(CTX, async () => {
        const take = driver.snapshot?.take;
        if (take === undefined) throw new Error("e2b captures");
        return take(box.id, crypto.randomUUID());
      });
      const ref = unwrap(made).ref;
      const wrong = await sandbox.restore(
        ref,
        "0".repeat(64),
        crypto.randomUUID(),
        CTX,
      );
      expect(code(wrong)).toBe("snapshot_manifest_mismatch");
      unwrap(await sandbox.release(ref, CTX));
    } finally {
      unwrap(await box.close(CTX));
    }
  });
});
