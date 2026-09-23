import { describe, expect, test } from "bun:test";
import { CTX } from "../../core/test/sandbox/context";
import { run } from "../../core/test/sandbox/remote/kit";
import { code, unwrap } from "../../core/test/store/helpers";
import { daytona } from "../src";

// Live gate: real Daytona, so it runs only with THREADS_LIVE=1 and
// DAYTONA_API_KEY (DAYTONA_API_URL optional).

const key = process.env["DAYTONA_API_KEY"];
const url = process.env["DAYTONA_API_URL"];
const live = process.env["THREADS_LIVE"] === "1" && key !== undefined;
const utf8 = new TextEncoder();

describe.skipIf(!live)("live gate: daytona", () => {
  const sandbox = () =>
    daytona({
      apiKey: key ?? "",
      ...(url === undefined ? {} : { apiUrl: url }),
    });

  test("create, exec, files, terminate and close against real Daytona", async () => {
    const adapter = sandbox();
    const box = unwrap(await adapter.create(crypto.randomUUID(), CTX));
    try {
      const out = await run(
        box,
        ["sh", "-c", "echo out; echo err >&2; exit 3"],
        { env: { PATH: "/usr/bin:/bin" } },
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
    expect(code(await adapter.attach(box.id, CTX))).toBe("not_found");
  });

  test("a cold snapshot restores a verified, isolated child", async () => {
    const adapter = sandbox();
    const box = unwrap(await adapter.create(crypto.randomUUID(), CTX));
    try {
      unwrap(await box.upload("f.txt", utf8.encode("one"), CTX));
      const snap = unwrap(await box.snapshot(crypto.randomUUID(), CTX));
      const child = unwrap(
        await adapter.restore(
          snap.snapshot_id,
          snap.manifest_hash,
          crypto.randomUUID(),
          CTX,
        ),
      );
      try {
        unwrap(await child.upload("f.txt", utf8.encode("two"), CTX));
        expect(unwrap(await box.download("f.txt", CTX))).toEqual(
          utf8.encode("one"),
        );
      } finally {
        unwrap(await child.close(CTX));
        unwrap(await adapter.release(snap.snapshot_id, CTX));
      }
    } finally {
      unwrap(await box.close(CTX));
    }
  });
});
