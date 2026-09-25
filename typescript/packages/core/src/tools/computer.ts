import type { z } from "zod";
import type { ToolRun } from "../loop/types";
import { containsSecret } from "../redact";
import { execute, toolRunOf } from "../sandbox/exec";
import {
  type Builtin,
  type BuiltinEnv,
  builtin,
  done,
  failed,
  sessionOf,
} from "./builtin";
import { ComputerInput, ComputerScreenshotInput } from "./sandbox-inputs";

// Computer use on the sandbox desktop (info.desktop native or image), driven
// by exec of xdotool and ImageMagick on the image's X display, so every desktop provider
// answers the same way. A screenshot is read_only and returns an image_ref part; an action is
// unguarded, because a click can submit a form somewhere else. The loop runs calls one at a
// time in model order, so GUI tools never run in parallel.

const DISPLAY = { DISPLAY: ":0" };
const SHOT = "/tmp/.threads-screenshot.png";
const TIMEOUT_MS = 30_000;
const PNG = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
const SCROLL = { up: "4", down: "5", left: "6", right: "7" } as const;

type Action = z.infer<typeof ComputerInput>;
type Shot = z.infer<typeof ComputerScreenshotInput>;

/** Width and height from a PNG's IHDR, or undefined when the bytes aren't a PNG. */
export function pngSize(
  bytes: Uint8Array,
): { readonly width: number; readonly height: number } | undefined {
  if (bytes.length < 24 || PNG.some((b, i) => bytes[i] !== b)) return undefined;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const width = view.getUint32(16);
  const height = view.getUint32(20);
  return width > 0 && height > 0 ? { width, height } : undefined;
}

/** The pointer position an action starts from, when x and y are given. */
const start = (a: Action): readonly string[] | undefined =>
  a.x === undefined || a.y === undefined
    ? undefined
    : ["mousemove", String(a.x), String(a.y)];

type Argv = readonly string[] | string;

const click =
  (button: string, repeat: readonly string[]) =>
  (a: Action): Argv => {
    const at = start(a);
    return at === undefined
      ? `${a.action} needs x and y`
      : [...at, "click", ...repeat, button];
  };

/** Per action, its xdotool argv or why the input can't make one; exhaustive by type. */
const ARGV: Readonly<Record<Action["action"], (a: Action) => Argv>> = {
  click: click("1", []),
  double_click: click("1", ["--repeat", "2"]),
  right_click: click("3", []),
  move: (a) => start(a) ?? "move needs x and y",
  drag: (a) => {
    const at = start(a);
    if (at === undefined || a.to_x === undefined || a.to_y === undefined)
      return "drag needs x, y, to_x and to_y";
    const end = ["mousemove", String(a.to_x), String(a.to_y)];
    return [...at, "mousedown", "1", ...end, "mouseup", "1"];
  },
  scroll: (a) => {
    const at = start(a);
    if (at === undefined || a.direction === undefined)
      return "scroll needs x, y and direction";
    const clicks = String(a.amount ?? 3);
    return [...at, "click", "--repeat", clicks, SCROLL[a.direction]];
  },
  type: (a) =>
    a.text === undefined
      ? "type needs text"
      : ["type", "--delay", "12", "--", a.text],
  key: (a) => (a.text === undefined ? "key needs text" : ["key", "--", a.text]),
};

/** The xdotool argv for an action, or why the input can't make one. */
export function xdotool(a: Action): Argv {
  return ARGV[a.action](a);
}

/** The capture command: the cursor on stdout, then the (cropped) PNG written to SHOT. */
export function captureScript(s: Shot): string | undefined {
  const region = [s.x, s.y, s.to_x, s.to_y];
  const given = region.filter((v) => v !== undefined).length;
  if (given !== 0 && given !== 4) return undefined;
  const [x = 0, y = 0, toX = 0, toY = 0] = region;
  if (given === 4 && (toX <= x || toY <= y)) return undefined;
  const crop =
    given === 4 ? ` -crop ${toX - x}x${toY - y}+${x}+${y} +repage` : "";
  const wait = s.wait_ms === undefined ? "" : `sleep ${s.wait_ms / 1000}; `;
  return `${wait}xdotool getmouselocation --shell && import -window root${crop} ${SHOT}`;
}

async function capture(
  input: Shot,
  key: string,
  env: BuiltinEnv,
): Promise<ToolRun> {
  const script = captureScript(input);
  if (script === undefined)
    return done(
      "a zoom region needs x < to_x and y < to_y, all four given",
      true,
    );
  const session = await sessionOf(env);
  if (!session.ok) return session.error;
  const ran = await execute(
    session.value,
    ["bash", "-c", script],
    env.context,
    {
      env: DISPLAY,
      timeoutMs: TIMEOUT_MS + (input.wait_ms ?? 0),
      processKey: key,
    },
    env.artifacts,
  );
  if (!ran.ok) return toolRunOf(ran);
  // The desktop isn't running or reachable: typed, never an empty success.
  if (ran.value.exit_code !== 0)
    return done(
      `unavailable: the desktop didn't answer: ${ran.value.stderr}`,
      true,
    );
  const got = await session.value.download(SHOT, env.context);
  if (!got.ok) return failed(got.error);
  // Byte-exact: an image holding a registered value (a text chunk) is refused, never stored.
  if (containsSecret(got.value))
    return done(
      "refused: the capture holds a registered secret; not stored",
      true,
    );
  const size = pngSize(got.value);
  if (size === undefined)
    return done("unavailable: the capture isn't a PNG", true);
  const cursor = /X=(\d+)\s+Y=(\d+)/.exec(ran.value.stdout);
  const said = `Screenshot ${size.width}x${size.height}${
    cursor === null ? "" : `; cursor at (${cursor[1]}, ${cursor[2]})`
  }.`;
  const ref = {
    sha256: await env.artifacts.put(got.value),
    bytes: got.value.length,
    media_type: "image/png",
  };
  return {
    kind: "done",
    output: said,
    isError: false,
    content: [
      { type: "text", text: said },
      { type: "image_ref", ref, ...size },
    ],
  };
}

export const computerScreenshot: Builtin = builtin({
  name: "computer_screenshot",
  input: ComputerScreenshotInput,
  effect: "read_only",
  run: async (input, ctx, env) => capture(input, ctx.effectKey, env),
});

export const computer: Builtin = builtin({
  name: "computer",
  input: ComputerInput,
  effect: "unguarded",
  run: async (input, ctx, env) => {
    const argv = xdotool(input);
    if (typeof argv === "string") return done(argv, true);
    const session = await sessionOf(env);
    if (!session.ok) return session.error;
    const ran = await execute(
      session.value,
      ["xdotool", ...argv],
      env.context,
      { env: DISPLAY, timeoutMs: TIMEOUT_MS, processKey: ctx.effectKey },
      env.artifacts,
    );
    // A timeout or a lost connection after dispatch is unknown and parks; never repeated.
    if (!ran.ok) return toolRunOf(ran);
    return ran.value.exit_code === 0
      ? done(`${input.action} done; take a screenshot to see the result`)
      : done(`${input.action} failed: ${ran.value.stderr}`, true);
  },
});
