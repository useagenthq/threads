import { describe, expect, test } from "bun:test";
import { createHash } from "node:crypto";
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
    const op = crypto.randomUUID();
    const box = unwrap(await adapter.create(op, CTX));
    try {
      if (adapter.lookup === undefined) throw new Error("daytona looks up");
      const found = unwrap(await adapter.lookup(op, CTX));
      expect(found.status === "found" && found.value.id).toBe(box.id);
      const record = await fetch(
        `${url ?? "https://app.daytona.io/api"}/sandbox/${box.id}`,
        { headers: { Authorization: `Bearer ${key}` } },
      );
      expect(await record.json()).toMatchObject({
        public: false,
        networkBlockAll: true,
        autoStopInterval: 60,
        autoDeleteInterval: 60,
      });
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

  // The qualification gate for: after toolbox traffic, no process env or command
  // line and no file in the guest holds the API key. The key never goes in to search for it:
  // the guest's state comes out and is searched on the host. The key's hash, written as a
  // probe file, must be found, which proves the scan reaches files.
  test("credential canary: the API key is nowhere in the guest", async () => {
    const adapter = sandbox();
    const box = unwrap(await adapter.create(crypto.randomUUID(), CTX));
    try {
      const probe = createHash("sha256")
        .update(key ?? "")
        .digest("hex");
      unwrap(await box.upload("probe.txt", utf8.encode(probe), CTX));
      await run(box, ["/bin/sh", "-c", "echo toolbox traffic"]);
      const dump = unwrap(
        await box.exec(
          [
            "/bin/sh",
            "-c",
            // Compressed in the guest: the hex-framed log stream carries ~170 KB/s, too slow
            // for the raw tens of MB.
            "{ cat /proc/[0-9]*/environ /proc/[0-9]*/cmdline 2>/dev/null; tar -cf - /etc /home /root /tmp /workspace /var /opt /run 2>/dev/null; } | gzip -1",
          ],
          CTX,
          { processKey: "canary", env: { PATH: "/usr/bin:/bin" } },
        ),
      );
      const [out] = await Promise.all([
        Array.fromAsync(dump.stdout),
        Array.fromAsync(dump.stderr),
      ]);
      await dump.exit_code;
      const guest = Buffer.from(Bun.gunzipSync(Buffer.concat(out)));
      expect(guest.includes(probe)).toBe(true);
      expect(guest.includes(key ?? "")).toBe(false);
    } finally {
      unwrap(await box.close(CTX));
    }
  });
});
