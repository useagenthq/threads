import { describe, expect, test } from "bun:test";
import { joined } from "../../core/src/sandbox/remote/bytes";
import { CTX } from "../../core/test/sandbox/context";
import { drained, run } from "../../core/test/sandbox/remote/kit";
import { trees } from "../../core/test/sandbox/remote/trees";
import { code, unwrap } from "../../core/test/store/helpers";
import { docker } from "../src";
import { resolveSocket } from "../src/socket";

// Live gate: the real Docker Engine on this machine, so it runs only with THREADS_LIVE=1.
// A missing socket under that flag is a loud failure, not a skip: the CI job that sets it
// runs on a machine with Docker, and a silent skip there would be a green vacuum.
// Every container is removed, with its three volumes, in `finally`.

const live = process.env["THREADS_LIVE"] === "1";
const utf8 = new TextEncoder();
const MINUTE = 120_000;

describe.skipIf(!live)("live gate: docker", () => {
  test("the Engine socket is where discovery looks for it", () => {
    expect(resolveSocket()).toMatch(/docker\.sock$/);
  });

  test(
    "create, exec, files, terminate and release against the real daemon",
    async () => {
      const adapter = docker();
      await adapter.setup?.();
      const op = crypto.randomUUID();
      const box = unwrap(await adapter.create(op, CTX));
      try {
        if (adapter.lookup === undefined) throw new Error("docker looks up");
        const found = unwrap(await adapter.lookup(op, CTX));
        expect(found.status === "found" && found.value.id).toBe(box.id);

        expect(
          await run(box, ["sh", "-c", "echo out; echo err >&2; exit 3"], {
            env: { PATH: "/usr/bin:/bin" },
          }),
        ).toEqual({ exit: 3, stdout: "out\n", stderr: "err\n" });
        // Exactly the call's env: the image's own ENV reaches no command (invariant 4).
        expect(
          (await run(box, ["/usr/bin/env"], { env: { ONLY: "this" } })).stdout,
        ).toBe("ONLY=this\n");
        expect(
          (await run(box, ["/bin/cat"], { stdin: utf8.encode("from stdin") }))
            .stdout,
        ).toBe("from stdin");
        // uid 1000, a /workspace it owns, and a root filesystem it can't write.
        const who = await run(
          box,
          ["/bin/sh", "-c", "id -u; stat -c %u /workspace"],
          {
            env: { PATH: "/usr/bin:/bin" },
          },
        );
        expect(who.stdout).toBe("1000\n1000\n");
        const sealed = await run(
          box,
          [
            "/bin/sh",
            "-c",
            "touch /etc/x 2>&1; cat /proc/1/environ 2>&1; true",
          ],
          { env: { PATH: "/usr/bin:/bin" } },
        );
        expect(sealed.stdout).toContain("Read-only file system");
        expect(sealed.stdout).toContain("Permission denied");

        const bytes = new Uint8Array([0, 1, 2, 255]);
        unwrap(await box.upload("a/b.bin", bytes, CTX));
        expect(unwrap(await box.download("a/b.bin", CTX))).toEqual(bytes);
        expect(code(await box.download("missing", CTX))).toBe("not_found");

        // A long command is terminated, and the answer is confirmed, not guessed. Its first
        // byte of output proves the supervisor recorded it: before the record there is
        // nothing to answer from, and terminate honestly says unknown (D-3).
        const held = unwrap(
          await box.exec(
            ["/bin/sh", "-c", "echo up; exec /bin/sleep 600"],
            CTX,
            { processKey: "held", env: { PATH: "/usr/bin:/bin" } },
          ),
        );
        for await (const _ of held.stdout) break;
        expect(unwrap(await box.terminate("held", CTX))).toBe("terminated");
        expect((await drained(held)).exit).not.toBe(0);
        expect(unwrap(await box.terminate("held", CTX))).toBe("terminated");
        // D-3, on purpose: a key that never ran has no record, so the call parks.
        expect(unwrap(await box.terminate("never-ran", CTX))).toBe("unknown");
      } finally {
        unwrap(await box.close(CTX));
      }
      expect(code(await adapter.attach(box.id, CTX))).toBe("not_found");
    },
    MINUTE,
  );

  test(
    "a tree exported from one container imports into another",
    async () => {
      const adapter = docker();
      const parent = unwrap(await adapter.create(crypto.randomUUID(), CTX));
      const child = unwrap(await adapter.create(crypto.randomUUID(), CTX));
      try {
        unwrap(await parent.upload("dir/f.txt", utf8.encode("one"), CTX));
        const exported = unwrap(await trees(parent).exportTree(CTX));
        const tar = await joined(exported.stdout);
        expect(await exported.exit_code).toBe(0);
        unwrap(
          await trees(child).importTree(
            (async function* () {
              yield tar;
            })(),
            CTX,
          ),
        );
        expect(unwrap(await child.download("dir/f.txt", CTX))).toEqual(
          utf8.encode("one"),
        );
      } finally {
        unwrap(await child.close(CTX));
        unwrap(await parent.close(CTX));
      }
    },
    MINUTE,
  );
});
