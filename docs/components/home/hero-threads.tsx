"use client";

import { useEffect, useRef } from "react";

/*
 * The hero backdrop: long woven strands drifting behind the headline.
 *
 * Canvas 2D rather than a 3D library — this is two dozen stroked curves, and a canvas keeps it to
 * one composited layer. The wrapper paints a static brand wash in CSS, so with JS off (or if the
 * 2D context is refused) the hero still has a background; the canvas only ever adds to it.
 * Colours come from --thread-a / --thread-b, registered in theme.css so getComputedStyle resolves
 * them; light and dark differ only in the canvas opacity, which is plain CSS, so a theme toggle
 * needs no work here.
 */

const TAU = Math.PI * 2;
/** Each strand is drawn as this many segments, smoothed into quadratics. */
const SAMPLES = 34;
/** Retina buys nothing on soft 1px lines, and halves the pixels we push. */
const MAX_DPR = 1.6;

type Bundle = {
  /** Centre of the bundle, 0 (top) to 1 (bottom). */
  readonly cy: number;
  /** How far the strands spread apart at their widest. */
  readonly band: number;
  /** Where along the width the bundle pinches into a waist. */
  readonly waist: number;
  /** How far the whole bundle waves up and down. */
  readonly sway: number;
  readonly waves: number;
  /** Cycles per second: every one of these is slow enough to read as drift, not motion. */
  readonly speed: number;
};

/*
 * Three ribbons, each a tight sheaf of fibres rather than a wide comb, kept in the upper half
 * where the mask lets them through. Bands stay narrow on purpose: a wide one fills the frame and
 * the result reads as a wireframe mesh instead of threads.
 */
const BUNDLES: readonly Bundle[] = [
  { cy: 0.2, band: 0.52, waist: 0.3, sway: 0.06, waves: 0.7, speed: 0.011 },
  { cy: 0.3, band: 0.66, waist: 0.62, sway: 0.045, waves: 0.95, speed: -0.008 },
  { cy: 0.26, band: 0.4, waist: 0.46, sway: 0.08, waves: 0.55, speed: 0.005 },
];

type Strand = {
  readonly bundle: Bundle;
  readonly offset: number;
  readonly phase: number;
  readonly ripple: number;
  readonly width: number;
  readonly alpha: number;
  readonly light: boolean;
};

/** Stable pseudo-random in 0..1, so resizing never reshuffles the weave. */
function noise(i: number): number {
  const v = Math.sin(i * 127.1 + 311.7) * 43758.5453;
  return v - Math.floor(v);
}

function makeStrands(perBundle: number): readonly Strand[] {
  return BUNDLES.flatMap((bundle, b) =>
    Array.from({ length: perBundle }, (_, j): Strand => {
      const seed = b * 37 + j;
      const t = perBundle === 1 ? 0.5 : j / (perBundle - 1);
      const n = noise(seed);
      return {
        bundle,
        offset: (t - 0.5) * bundle.band,
        phase: n,
        ripple: 0.008 + noise(seed + 5) * 0.022,
        width: 0.7 + noise(seed + 11) * 1.0,
        alpha: 0.25 + noise(seed + 17) * 0.6,
        light: noise(seed + 23) > 0.7,
      };
    }),
  );
}

function strandPath(s: Strand, time: number, w: number, h: number): Path2D {
  const b = s.bundle;
  const reach = Math.max(b.waist, 1 - b.waist) ** 2;
  // Vertical distances scale with the width, not the height. The hero is far taller than it is
  // wide on a phone, and measuring against the height there turns gentle strands into steep spikes.
  const amp = Math.min(h, w * 1.5);
  // A slow vertical breath on top of the sway, so bundles never move in lockstep.
  const breathe = 0.016 * Math.sin(TAU * (time * 0.004 + s.phase));
  const at = (i: number): readonly [number, number] => {
    // Run past both edges so a strand never ends inside the frame.
    const x = -0.06 + (i / SAMPLES) * 1.12;
    const d = x - b.waist;
    // Wide at the ends, pinched to a sixth of that at the waist: a bundle, not a comb.
    const env = 0.16 + 0.84 * ((d * d) / reach);
    const spine = b.cy + breathe + b.sway * Math.sin(TAU * (x * b.waves + s.phase + time * b.speed));
    const ripple = s.ripple * Math.sin(TAU * (x * 2.4 + s.phase * 3 + time * b.speed * 1.7));
    return [x * w, (spine + (s.offset + ripple) * env) * amp];
  };

  const path = new Path2D();
  let [px, py] = at(0);
  path.moveTo(px, py);
  for (let i = 1; i < SAMPLES; i++) {
    const [x, y] = at(i);
    path.quadraticCurveTo(px, py, (px + x) / 2, (py + y) / 2);
    [px, py] = [x, y];
  }
  const [lx, ly] = at(SAMPLES);
  path.quadraticCurveTo(px, py, lx, ly);
  return path;
}

type Palette = { readonly a: string; readonly b: string };

/**
 * --thread-a / --thread-b are registered with @property, so getComputedStyle hands back a resolved
 * colour rather than the literal color-mix(). A browser without @property returns something canvas
 * refuses, which the probe below catches: we then leave the canvas empty and the CSS wash stands in.
 */
function readPalette(ctx: CanvasRenderingContext2D, el: Element): Palette | undefined {
  const style = getComputedStyle(el);
  const accepted = (name: string): string | undefined => {
    const raw = style.getPropertyValue(name).trim();
    ctx.strokeStyle = "#010203";
    ctx.strokeStyle = raw;
    return ctx.strokeStyle === "#010203" ? undefined : raw;
  };
  const a = accepted("--thread-a");
  const b = accepted("--thread-b");
  return a && b ? { a, b } : undefined;
}

function draw(
  ctx: CanvasRenderingContext2D,
  strands: readonly Strand[],
  palette: Palette,
  time: number,
  w: number,
  h: number,
): void {
  ctx.clearRect(0, 0, w, h);
  ctx.lineCap = "round";
  for (const s of strands) {
    const path = strandPath(s, time, w, h);
    // Clear a gap under each strand before drawing it, so crossings read as over and under
    // instead of piling up into a brighter mesh.
    ctx.globalCompositeOperation = "destination-out";
    ctx.globalAlpha = 0.9;
    ctx.lineWidth = s.width + 3.5;
    ctx.stroke(path);
    ctx.globalCompositeOperation = "source-over";
    ctx.globalAlpha = s.alpha;
    ctx.strokeStyle = s.light ? palette.b : palette.a;
    ctx.lineWidth = s.width;
    ctx.stroke(path);
  }
}

export function HeroThreads(): React.ReactElement {
  const host = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const el = canvas.current;
    const box = host.current;
    if (!el || !box) return;
    const ctx = el.getContext("2d");
    if (!ctx) return;

    const palette = readPalette(ctx, el);
    if (!palette) return;
    const stillOnly = matchMedia("(prefers-reduced-motion: reduce)");

    let strands: readonly Strand[] = [];
    let width = 0;
    let height = 0;
    let time = 0;
    let last = 0;
    let frame = 0;
    let onScreen = true;

    const render = (): void => {
      if (width > 0 && height > 0) draw(ctx, strands, palette, time, width, height);
    };

    const resize = (): void => {
      const rect = box.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) return;
      const dpr = Math.min(devicePixelRatio || 1, MAX_DPR);
      [width, height] = [rect.width, rect.height];
      el.width = Math.round(width * dpr);
      el.height = Math.round(height * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      // Thin the sheaf on a narrow screen: the same picture with fewer strokes per frame.
      strands = makeStrands(width < 640 ? 5 : width < 1024 ? 6 : 7);
      render();
    };

    const tick = (now: number): void => {
      frame = requestAnimationFrame(tick);
      // Clamp the step so a backgrounded tab does not resume with a jump.
      time += Math.min(now - last, 100) / 1000;
      last = now;
      render();
    };

    const shouldRun = (): boolean => onScreen && document.visibilityState === "visible" && !stillOnly.matches;

    const sync = (): void => {
      if (shouldRun()) {
        if (frame === 0) {
          last = performance.now();
          frame = requestAnimationFrame(tick);
        }
      } else if (frame !== 0) {
        cancelAnimationFrame(frame);
        frame = 0;
        render();
      }
    };

    const sizeWatcher = new ResizeObserver(resize);
    sizeWatcher.observe(box);
    const viewWatcher = new IntersectionObserver((entries) => {
      onScreen = entries.some((e) => e.isIntersecting);
      sync();
    });
    viewWatcher.observe(box);
    document.addEventListener("visibilitychange", sync);
    stillOnly.addEventListener("change", sync);

    resize();
    sync();

    return () => {
      if (frame !== 0) cancelAnimationFrame(frame);
      sizeWatcher.disconnect();
      viewWatcher.disconnect();
      document.removeEventListener("visibilitychange", sync);
      stillOnly.removeEventListener("change", sync);
    };
  }, []);

  return (
    <div ref={host} aria-hidden="true" className="hero-threads">
      <canvas ref={canvas} />
    </div>
  );
}
