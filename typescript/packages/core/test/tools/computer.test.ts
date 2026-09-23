import { describe, expect, test } from "bun:test";
import { agent, fakeSandbox, scriptedModel, sqlite } from "../../src";
import { openStore } from "../../src/agent/sqlite";
import { type KnownEvent, SandboxId } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { err, ok } from "../../src/result";
import type { Sandbox, SandboxSession } from "../../src/sandbox/protocol";
import {
  captureScript,
  computerScreenshot,
  pngSize,
  xdotool,
} from "../../src/tools/computer";
import { ComputerInput } from "../../src/tools/sandbox-inputs";
import { unwrap } from "../store/helpers";
import { bound } from "./kit";

// Computer use (F11.8, F11.9): a screenshot is a read_only image_ref result;
// an action is unguarded and parks when its outcome is unknown; no desktop is a setup error.

/** A PNG header for a width x height image (enough for the IHDR read). */
function png(width: number, height: number): Uint8Array {
  const b = new Uint8Array(33);
  b.set([
    0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 13, 0x49, 0x48,
    0x44, 0x52,
  ]);
  const v = new DataView(b.buffer);
  v.setUint32(16, width);
  v.setUint32(20, height);
  return b;
}

const act = (raw: Record<string, unknown>) => xdotool(ComputerInput.parse(raw));

describe("computer argv", () => {
  test("each action's xdotool argv; missing coordinates are refused", () => {
    expect(act({ action: "click", x: 5, y: 6 })).toEqual([
      "mousemove",
      "5",
      "6",
      "click",
      "1",
    ]);
    expect(act({ action: "double_click", x: 5, y: 6 })).toEqual([
      "mousemove",
      "5",
      "6",
      "click",
      "--repeat",
      "2",
      "1",
    ]);
    expect(act({ action: "right_click", x: 1, y: 2 })).toEqual([
      "mousemove",
      "1",
      "2",
      "click",
      "3",
    ]);
    expect(act({ action: "drag", x: 1, y: 2, to_x: 3, to_y: 4 })).toEqual([
      "mousemove",
      "1",
      "2",
      "mousedown",
      "1",
      "mousemove",
      "3",
      "4",
      "mouseup",
      "1",
    ]);
    expect(act({ action: "scroll", x: 1, y: 2, direction: "up" })).toEqual([
      "mousemove",
      "1",
      "2",
      "click",
      "--repeat",
      "3",
      "4",
    ]);
    // Text is one argv element after --: never parsed by a shell or as an option.
    expect(act({ action: "type", text: "-rf; $(x)" })).toEqual([
      "type",
      "--delay",
      "12",
      "--",
      "-rf; $(x)",
    ]);
    expect(act({ action: "key", text: "ctrl+s" })).toEqual([
      "key",
      "--",
      "ctrl+s",
    ]);
    expect(act({ action: "click" })).toBe("click needs x and y");
    expect(act({ action: "drag", x: 1, y: 2 })).toBe(
      "drag needs x, y, to_x and to_y",
    );
    expect(act({ action: "type" })).toBe("type needs text");
  });

  test("capture script: whole screen, a zoom region, or a refusal", () => {
    expect(captureScript({})).toBe(
      "xdotool getmouselocation --shell && import -window root /tmp/.threads-screenshot.png",
    );
    expect(
      captureScript({ x: 10, y: 20, to_x: 110, to_y: 70, wait_ms: 500 }),
    ).toBe(
      "sleep 0.5; xdotool getmouselocation --shell && import -window root -crop 100x50+10+20 +repage /tmp/.threads-screenshot.png",
    );
    expect(captureScript({ x: 1 })).toBeUndefined();
    expect(captureScript({ x: 5, y: 5, to_x: 5, to_y: 9 })).toBeUndefined();
  });

  test("pngSize reads IHDR and rejects non-PNG bytes", () => {
    expect(pngSize(png(1280, 800))).toEqual({ width: 1280, height: 800 });
    expect(
      pngSize(new TextEncoder().encode("GIF89a........................")),
    ).toBeUndefined();
  });
});

/** A session whose exec answers `stdout` and whose download returns `file`. */
function desktopSession(
  stdout: string,
  file: Uint8Array,
  code = 0,
): SandboxSession {
  const stream = async function* (s: string) {
    if (s !== "") yield new TextEncoder().encode(s);
  };
  return {
    id: SandboxId.parse("desk"),
    exec: async () =>
      ok({
        exit_code: Promise.resolve(code),
        stdout: stream(stdout),
        stderr: stream(code === 0 ? "" : "no display"),
      }),
    terminate: async () => ok("unknown"),
    upload: async () => ok(undefined),
    download: async () => ok(file),
    snapshot: async () => err({ code: "unavailable", message: "x" }),
    close: async () => ok(undefined),
  };
}

describe("computer_screenshot", () => {
  test("an image_ref part with width and height, the bytes an artifact, and the cursor", async () => {
    const tool = bound(
      computerScreenshot,
      desktopSession("X=640\nY=400\nSCREEN=0\n", png(1280, 800)),
    );
    const run = await tool.run({});
    if (run.kind !== "done") throw new Error(run.kind);
    expect(run.output).toBe("Screenshot 1280x800; cursor at (640, 400).");
    const image = run.content?.[1];
    expect(image).toMatchObject({
      type: "image_ref",
      width: 1280,
      height: 800,
      ref: { media_type: "image/png", bytes: 33 },
    });
    if (image?.type !== "image_ref") throw new Error("no image");
    expect(tool.artifacts.get(image.ref.sha256).ok).toBe(true);
  });

  test("no desktop answering is unavailable, never an empty success", async () => {
    const run = await bound(
      computerScreenshot,
      desktopSession("", new Uint8Array(0), 1),
    ).run({});
    expect(run).toMatchObject({ kind: "done", isError: true });
    expect(run.kind === "done" && run.output.startsWith("unavailable:")).toBe(
      true,
    );
    const notPng = await bound(
      computerScreenshot,
      desktopSession("", new Uint8Array(40)),
    ).run({});
    expect(notPng).toMatchObject({
      isError: true,
      output: "unavailable: the capture isn't a PNG",
    });
  });
});

/** The fake provider with a desktop; its exec is lost after dispatch when `lost`. */
function desktop(lost: boolean): Sandbox {
  const fake = fakeSandbox({ tools: { xdotool: { output: "" } } });
  const broken = (s: SandboxSession): SandboxSession =>
    lost
      ? {
          ...s,
          exec: async () =>
            err({ code: "unavailable", message: "connection lost" }),
        }
      : s;
  return {
    ...fake,
    info: { ...fake.info, desktop: "native" },
    create: async (key, ctx) => {
      const made = await fake.create(key, ctx);
      return made.ok ? ok(broken(made.value)) : made;
    },
  };
}

async function eventsOf(lost: boolean): Promise<readonly KnownEvent[]> {
  const usage = { input_tokens: 1, output_tokens: 1 };
  const model = scriptedModel({
    responses: [
      {
        content: [
          {
            type: "tool_use",
            call_id: "c1",
            name: "computer",
            input: { action: "click", x: 1, y: 2 },
          },
        ],
        stop_reason: "tool_use",
        usage,
      },
      {
        content: [{ type: "text", text: "ok" }],
        stop_reason: "end_turn",
        usage,
      },
    ],
  });
  const result = await agent({
    model,
    sandbox: desktop(lost),
    computer: true,
    permissions: { mode: "bypass", allow_bypass: true },
  }).run("click it", { store: sqlite(":memory:") });
  const { log } = await openStore(result.thread.store);
  return knownEvents(unwrap(log.read(result.thread.branch)));
}

describe("computer in a run", () => {
  test("pins computer (unguarded) and computer_screenshot (read_only)", async () => {
    const events = await eventsOf(false);
    const started = events.find((e) => e.type === "thread_started");
    const tools = started?.type === "thread_started" ? started.data.tools : [];
    expect(
      tools
        .filter((t) => t.name.startsWith("computer"))
        .map((t) => [t.name, t.effect_class]),
    ).toEqual([
      ["computer", "unguarded"],
      ["computer_screenshot", "read_only"],
    ]);
    const shown = events.find((e) => e.type === "tool_result");
    expect(shown?.type === "tool_result" && shown.data.preview).toBe(
      "click done; take a screenshot to see the result",
    );
  });

  test("a click whose outcome is lost is effect_unknown and parks; it is never repeated", async () => {
    const events = await eventsOf(true);
    const types = events.map((e) => e.type);
    expect(types).toContain("effect_unknown");
    expect(events.filter((e) => e.type === "effect_begin").length).toBe(1);
    const parked = events.find((e) => e.type === "parked");
    expect(parked?.type === "parked" && parked.data.reason).toBe(
      "effect_unknown",
    );
    expect(types).not.toContain("tool_result");
  });

  test("computer without a desktop is a setup error naming the capability", async () => {
    const model = scriptedModel({ responses: [] });
    await expect(
      agent({ model, sandbox: fakeSandbox(), computer: true }).check(),
    ).resolves.toMatchObject({
      ok: false,
      error: { code: "capability_missing" },
    });
    await expect(
      agent({ model, computer: true }).check(),
    ).resolves.toMatchObject({ ok: false });
  });
});
