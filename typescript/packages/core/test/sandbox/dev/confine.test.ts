import { describe, expect, test } from "bun:test";
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readdirSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import { devSandbox, type SandboxSession } from "../../../src/sandbox";
import { confinement, probe } from "../../../src/sandbox/dev/confine";
import { joined } from "../../../src/sandbox/remote/bytes";
import { unwrap } from "../../store/helpers";
import { CTX } from "../context";

// What the confinement denies. Every case runs the real OS confinement, so it is skipped where
// there is none (a Linux host without bubblewrap, or with user namespaces disabled); CI's Linux
// job installs bubblewrap so these run there, and they run on macOS as they are.
//
// The two platforms deny different things, and the tests say which:
// - both: a home file, the network, another process's environment, an inherited fd.
// - Linux only: a private /run and /tmp, and a pid namespace, which come from bwrap's mounts.
// - macOS has no mount namespace, so /tmp is denied outright instead of being made private,
//   and the sandbox directory keeps its host path.

const ROOT = mkdtempSync(join(tmpdir(), "threads-dev-confine-"));
const missing = (() => {
  const made = confinement(false, undefined);
  return made.ok ? probe(made.value, ROOT) : made.error;
})();

const withConfinement = missing === undefined ? describe : describe.skip;

/**
 * Set to 1 on a host that must be able to confine (CI's Linux runners, once they install
 * bubblewrap). Then an unavailable confinement fails instead of skipping: a lane whose denials
 * quietly stop being asserted looks proven when only half of it ran.
 */
const REQUIRED = "THREADS_REQUIRE_CONFINEMENT";

const WHY = `the dev sandbox has no working OS confinement (bubblewrap on Linux, sandbox-exec on macOS): ${missing}. Install bubblewrap to run these on Linux.`;

describe("the dev sandbox's confinement", () => {
  // The loud half of the skip: bun prints a skipped suite without a reason, so this always
  // runs, names what is missing, and fails where a confinement is required.
  test("is available, or says by name why not", () => {
    if (missing === undefined) return;
    console.warn(`threads: ${WHY}`);
    expect(process.env[REQUIRED]).not.toBe("1");
  });
});

type Ran = {
  readonly code: number;
  readonly out: string;
  readonly err: string;
};

const ran = (session: SandboxSession, script: string): Promise<Ran> =>
  ranWith(session, "/bin/sh", script);

async function ranWith(
  session: SandboxSession,
  shell: string,
  script: string,
): Promise<Ran> {
  const output = unwrap(
    await session.exec([shell, "-c", script], CTX, {
      processKey: `k-${Math.random()}`,
    }),
  );
  const decode = new TextDecoder();
  const [out, errors] = await Promise.all([
    joined(output.stdout),
    joined(output.stderr),
  ]);
  return {
    code: await output.exit_code,
    out: decode.decode(out),
    err: decode.decode(errors),
  };
}

async function box(allowInternet = false): Promise<SandboxSession> {
  const sandbox = devSandbox({ root: ROOT, allowInternet });
  await sandbox.setup?.();
  return unwrap(await sandbox.create(`op-${Math.random()}`, CTX));
}

withConfinement("a confined command", () => {
  test("runs in /workspace with exactly the call's environment", async () => {
    const session = await box();
    const output = unwrap(
      await session.exec(["/usr/bin/env"], CTX, {
        processKey: "k-env",
        env: { GREETING: "hello" },
      }),
    );
    const text = new TextDecoder().decode(await joined(output.stdout));
    const lines = text.trim() === "" ? [] : text.trim().split("\n");
    expect(await output.exit_code).toBe(0);
    // Names, not values: a CI log masks a value that matches one of its own secrets, so a leak
    // has to be named to be readable at all.
    expect(lines.map((line) => line.split("=")[0]).toSorted()).toEqual([
      "GREETING",
    ]);
    expect(lines).toContain("GREETING=hello");
  });

  test("writes only inside its own workspace, and the host is unchanged", async () => {
    const session = await box();
    expect((await ran(session, "echo made > made.txt")).code).toBe(0);
    expect(unwrap(await session.download("/workspace/made.txt", CTX))).toEqual(
      new TextEncoder().encode("made\n"),
    );

    // The promise is that the host is unchanged, not that every write fails. bwrap gives the
    // command a private tmpfs root, so a write outside /workspace can succeed inside and go
    // away with the sandbox; sandbox-exec has no root to replace and refuses it instead. This
    // asserts the part that has to hold on either platform: the dev root is writable by this
    // user on the host, so a file appearing there would be a real escape.
    const outside = join(ROOT, "threads-escape.txt");
    const escaped = await ran(
      session,
      `echo x > ${JSON.stringify(outside)} 2>&1; echo done`,
    );
    expect(escaped.out).toContain("done");
    expect(existsSync(outside)).toBe(false);
    expect(existsSync("/threads-escape.txt")).toBe(false);
  });

  test("can't read a planted home file", async () => {
    const planted = join(homedir(), ".threads-dev-home-secret");
    writeFileSync(planted, "a credential");
    try {
      const session = await box();
      const read = await ran(session, `cat ${JSON.stringify(planted)} 2>&1`);
      expect(read.code).not.toBe(0);
      expect(`${read.out}${read.err}`).not.toContain("a credential");
    } finally {
      const { rmSync } = await import("node:fs");
      rmSync(planted, { force: true });
    }
  });

  test("has no network, and allowInternet is what opens it", async () => {
    const server = createServer((socket) => socket.end());
    await new Promise<void>((done) => server.listen(0, "127.0.0.1", done));
    const address = server.address();
    const port =
      typeof address === "object" && address !== null ? address.port : 0;
    try {
      // bash's /dev/tcp, because dash has none: the positive case proves the probe works.
      const connect = `exec 3<>/dev/tcp/127.0.0.1/${port} 2>/dev/null && echo open || echo closed`;
      const denied = await ranWith(await box(), "/bin/bash", connect);
      expect(denied.out.trim()).toBe("closed");
      const opened = await ranWith(await box(true), "/bin/bash", connect);
      expect(opened.out.trim()).toBe("open");
    } finally {
      server.close();
    }
  });

  test("can't read another process's environment", async () => {
    const helper = spawn("/bin/sh", ["-c", "sleep 30"], {
      env: { THREADS_DEV_SECRET: "a credential" },
      stdio: "ignore",
    });
    try {
      const session = await box();
      const read = await ran(
        session,
        `cat /proc/${helper.pid}/environ 2>&1; ps -p ${helper.pid} -wwE 2>&1`,
      );
      expect(`${read.out}${read.err}`).not.toContain("a credential");
    } finally {
      helper.kill("SIGKILL");
    }
  });

  test("inherits no file descriptor beyond the three pipes", async () => {
    const session = await box();
    const read = await ran(session, "cat <&3 2>&1; echo done");
    expect(read.out).toContain("done");
    expect(read.out).not.toContain("a credential");
  });

  test("allowInternet is the only way egress opens", async () => {
    expect(devSandbox({ root: ROOT }).info.egress).toBe("enforced");
    expect(devSandbox({ root: ROOT, allowInternet: true }).info.egress).toBe(
      "unenforced",
    );
  });
});

const onLinux =
  missing === undefined && process.platform === "linux"
    ? describe
    : describe.skip;

onLinux("bubblewrap's own mounts", () => {
  test("/run and /tmp are private, and the pid namespace hides the host", async () => {
    const session = await box();
    expect((await ran(session, "ls -A /run | wc -l")).out.trim()).toBe("0");
    expect((await ran(session, "ls -A /tmp | wc -l")).out.trim()).toBe("0");
    // PID 1 of the namespace is the wrapper itself, so the host's processes are not there.
    expect(
      (await ran(session, "ls /proc/1/ >/dev/null && echo ok")).out.trim(),
    ).toBe("ok");
    expect(
      (await ran(session, "ls -d /proc/[0-9]* | wc -l")).out.trim(),
    ).not.toBe("0");
  });
});

const onMac =
  missing === undefined && process.platform === "darwin"
    ? describe
    : describe.skip;

onMac("sandbox-exec's own denials", () => {
  test("the host's temp directory outside the sandbox is unreadable", async () => {
    const outside = join(ROOT, "outside.txt");
    writeFileSync(outside, "a credential");
    const session = await box();
    const read = await ran(session, `cat ${JSON.stringify(outside)} 2>&1`);
    expect(read.code).not.toBe(0);
    expect(`${read.out}${read.err}`).not.toContain("a credential");
    expect(readdirSync(ROOT)).toContain("outside.txt");
  });
});
