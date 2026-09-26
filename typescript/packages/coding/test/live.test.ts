import { describe, expect, test } from "bun:test";
import { docker } from "@threads/docker";
import { CTX } from "../../core/test/sandbox/context";
import { run } from "../../core/test/sandbox/remote/kit";
import { unwrap } from "../../core/test/store/helpers";
import { CODING_INSTRUCTIONS } from "../src";

// Live gate: the real Docker Engine on this machine, so it runs only with THREADS_LIVE=1. A
// missing daemon under that flag is a loud failure, not a skip: the docker workflow that sets it
// installs one. What is proven here can't be faked — real git in the default image, the cgroup
// limits the daemon really applied, and an environment the container cannot see.
//
// The sandbox is built here rather than taken off codingAgent()'s handle, which exposes only
// {name, run, stream, check}. The numbers are the preset's (src/index.ts), and the offline suite
// pins that the preset's pin is the same agent() pin as docker({cpus: 2, memoryMb: 4096}).

const live = process.env["THREADS_LIVE"] === "1";
const MINUTE = 120_000;
const utf8 = new TextEncoder();
const PATH = { PATH: "/usr/local/bin:/usr/bin:/bin" };

/** The two commands CODING_INSTRUCTIONS tells the model to run, taken from the constant. */
const BASELINE =
  "git init -q; git add -A && git -c user.name=threads -c user.email=threads@localhost commit -q --no-verify --allow-empty -m threads-baseline && git tag -f threads-baseline";
const DIFF =
  "git add -A && git add -A --renormalize && git diff --cached --binary threads-baseline";

const sh = (session: Parameters<typeof run>[0], script: string) =>
  run(session, ["/bin/sh", "-c", script], { env: PATH });

async function box() {
  const sandbox = docker({ cpus: 2, memoryMb: 4096 });
  await sandbox.setup?.();
  return unwrap(await sandbox.create(crypto.randomUUID(), CTX));
}

/**
 * src/app.ts, plus the edit, the new file and the binary a real change leaves behind. The edit is
 * exactly as long as what it replaces, on purpose: an uploaded file lands with mtime 0, so that is
 * the case git's stat cache hides and the instructions carry `--renormalize` for.
 */
async function changed(
  session: Awaited<ReturnType<typeof box>>,
): Promise<string> {
  unwrap(await session.upload("src/app.ts", utf8.encode("new\n"), CTX));
  unwrap(await session.upload("src/new.ts", utf8.encode("added\n"), CTX));
  expect(
    (await sh(session, "printf '\\000\\001\\002\\377' > logo.bin")).exit,
  ).toBe(0);
  const diff = await sh(session, DIFF);
  expect(diff.exit).toBe(0);
  return diff.stdout;
}

// Not gated: it needs no daemon, and it is what ties the commands below to the constant.
test("the commands proven below are the ones the instructions name", () => {
  expect(CODING_INSTRUCTIONS).toContain(BASELINE);
  expect(CODING_INSTRUCTIONS).toContain(DIFF);
});

describe.skipIf(!live)(
  "live gate: what the coding instructions ask the model to run",
  () => {
    test(
      "the diff carries an edit, a new file and a binary, whether or not /workspace was a repo",
      async () => {
        // A repository with a commit and a pre-commit hook that always fails, and a plain
        // directory: the baseline is recorded either way.
        const existing = [
          "cd /workspace",
          "git init -q",
          "git add -A",
          "git -c user.name=t -c user.email=t@l commit -q -m first",
          "printf '#!/bin/sh\\nexit 1\\n' > .git/hooks/pre-commit",
          "chmod +x .git/hooks/pre-commit",
        ].join(" && ");
        for (const before of ["true", existing]) {
          const session = await box();
          try {
            unwrap(
              await session.upload("src/app.ts", utf8.encode("old\n"), CTX),
            );
            expect((await sh(session, before)).exit).toBe(0);
            expect((await sh(session, BASELINE)).exit).toBe(0);
            const diff = await changed(session);
            expect(diff).toContain("-old");
            expect(diff).toContain("+new");
            expect(diff).toContain("new file mode");
            expect(diff).toContain("src/new.ts");
            expect(diff).toContain("GIT binary patch");
          } finally {
            unwrap(await session.close(CTX));
          }
        }
      },
      MINUTE,
    );

    test(
      "the default image carries git, the limits bind, and the host's key never arrives",
      async () => {
        const had = process.env["ANTHROPIC_API_KEY"];
        process.env["ANTHROPIC_API_KEY"] = "sk-ant-not-a-real-key";
        const session = await box();
        try {
          const version = await sh(session, "git --version");
          expect(version.stdout).toContain("git version");

          // The limits codingAgent() asks for, as the daemon applied them (cgroup v2).
          const limits = await sh(
            session,
            "cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/cpu.max /sys/fs/cgroup/pids.max",
          );
          expect(limits.stdout.split("\n").slice(0, 3)).toEqual([
            "4294967296",
            "200000 100000",
            "1024",
          ]);

          // Invariant 4: exactly the call's environment, and PID 1's is unreadable.
          const env = await run(session, ["/usr/bin/env"], {
            env: { ONLY: "this" },
          });
          expect(env.stdout).toBe("ONLY=this\n");
          // Names, not values: a CI log masks a value it knows, so a leak has to be named to read.
          expect(env.stdout).not.toContain("ANTHROPIC_API_KEY");
          const sealed = await sh(session, "cat /proc/1/environ 2>&1; true");
          expect(sealed.stdout).toContain("Permission denied");
          expect(sealed.stdout).not.toContain("ANTHROPIC_API_KEY");
        } finally {
          unwrap(await session.close(CTX));
          if (had === undefined) delete process.env["ANTHROPIC_API_KEY"];
          else process.env["ANTHROPIC_API_KEY"] = had;
        }
      },
      MINUTE,
    );
  },
);
