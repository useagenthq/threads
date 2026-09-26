"use client";

import { useState } from "react";
import recorded from "./recorded-run.json";
import { panel } from "./section";

/*
 * One real run, as thread.timeline() returned it.
 *
 * Nothing here is drawn. recorded-run.json is the output of thread.timeline() for a live
 * claude-sonnet-5 support agent that looks up an order, is told a refund needs a person, parks,
 * is approved, resumes and refunds. Every seq, type, field name and value below is read out of
 * that file, and the one-line note beside each row is derived from the event's own data rather
 * than written by hand, so the section cannot drift from the recording.
 *
 * The run has no snapshot event, so it has no fork point: the section claims neither.
 */

type Json = string | number | boolean | null | undefined | Json[] | { [k: string]: Json };
type Event = {
  readonly seq: number;
  readonly type: string;
  readonly epoch: number;
  readonly actor: Json;
  readonly data: { readonly [k: string]: Json };
};

const EVENTS: readonly Event[] = recorded.entries.map((e) => e.event);
/** Where the run parks. Splitting here keeps the two columns even and puts the wait on the seam. */
const SPLIT = EVENTS.findIndex((e) => e.type === "parked") + 1;

function field(v: Json, key: string): Json {
  return typeof v === "object" && v !== null && !Array.isArray(v) ? v[key] : undefined;
}

function plain(v: Json): string {
  return typeof v === "string" ? v : (JSON.stringify(v) ?? "");
}

/** The note beside a row: real values from that event, never prose about it. */
const NOTE = new Map<string, (d: Event["data"]) => string>([
  ["thread_started", (d) => `${plain(d.agent_name)} · ${plain(field(d.model, "name"))}`],
  ["user_input", (d) => plain(d.text)],
  ["model_request", (d) => `${plain(field(d.request_ref, "bytes"))} bytes sent`],
  [
    "model_response",
    (d) => `${plain(d.stop_reason)} · ${plain(field(d.usage, "output_tokens"))} output tokens`,
  ],
  ["tool_call", (d) => `${plain(d.name)} ${plain(d.input)}`],
  ["permission_decision", (d) => `${plain(d.decision)} · source ${plain(d.source)}`],
  ["tool_result", (d) => plain(d.preview)],
  ["approval_requested", (d) => `args_hash ${plain(d.args_hash).slice(0, 8)}…`],
  ["parked", (d) => plain(d.reason)],
  ["approval_granted", (d) => `challenge ${plain(d.challenge_id).slice(0, 8)}…`],
  ["resumed", (d) => `address ${plain(field(d.address, "kind"))}`],
  ["effect_begin", (d) => `attempt ${plain(d.attempt)}`],
  ["effect_commit", (d) => `receipt ${plain(field(d.result_ref, "bytes"))} bytes`],
  ["turn_completed", (d) => plain(d.reason)],
]);

const MAX = 116;
function value(v: Json): string {
  const s = JSON.stringify(v) ?? "";
  return s.length > MAX ? `${s.slice(0, MAX)}…` : s;
}

/** What the log is for, in the reader's terms. Each one reads these same events. */
const USES: readonly { readonly call: string; readonly body: string }[] = [
  { call: "timeline()", body: "Read a bad answer back, months later, with no logging added." },
  { call: "run()", body: "Resume after a crash without sending the refund twice." },
  { call: "fork()", body: "Branch from an eligible snapshot and try another prompt from the same state." },
  { call: "saveCase()", body: "Keep a turn as a test that reruns with no model calls." },
];

function Row({ e, active, onSelect }: { e: Event; active: boolean; onSelect: (seq: number) => void }) {
  return (
    <button
      type="button"
      onClick={() => onSelect(e.seq)}
      aria-current={active ? "true" : undefined}
      className={`grid w-full cursor-pointer grid-cols-[1.4rem_minmax(0,auto)_minmax(0,1fr)] items-baseline gap-x-2 rounded border-l-2 py-[3px] pr-2 pl-1.5 text-left font-mono text-[0.72rem] leading-5 transition-colors sm:text-[0.76rem] ${
        active ? "border-fd-primary bg-fd-primary/10" : "border-transparent hover:bg-fd-accent/70"
      }`}
    >
      <span className="text-right text-fd-muted-foreground tabular-nums">{e.seq}</span>
      <span className={active ? "text-fd-primary" : "text-fd-foreground"}>{e.type}</span>
      <span className="min-w-0 truncate text-fd-muted-foreground">{NOTE.get(e.type)?.(e.data) ?? ""}</span>
    </button>
  );
}

function Column({
  label,
  aside,
  rows,
  selected,
  onSelect,
  className,
}: {
  label: string;
  aside: string;
  rows: readonly Event[];
  selected: number;
  onSelect: (seq: number) => void;
  className: string;
}) {
  return (
    <div className={`border-fd-border px-2 py-3 ${className}`}>
      <div className="px-1.5 pb-2">
        <p className="font-mono text-[0.68rem] tracking-widest text-fd-muted-foreground uppercase">{label}</p>
        <p className="mt-0.5 text-xs leading-5 text-fd-muted-foreground">{aside}</p>
      </div>
      <ol>
        {rows.map((e, i) => (
          <li key={e.seq}>
            {e.epoch !== (rows[i - 1]?.epoch ?? EVENTS[0]?.epoch) ? (
              <p className="mt-2 mb-1 flex items-center gap-2 px-1.5 font-mono text-[0.62rem] tracking-widest text-fd-muted-foreground uppercase">
                epoch {e.epoch}
                <span className="h-px flex-1 bg-fd-border" aria-hidden="true" />
              </p>
            ) : null}
            <Row e={e} active={e.seq === selected} onSelect={onSelect} />
          </li>
        ))}
      </ol>
    </div>
  );
}

export function Timeline() {
  const [selected, setSelected] = useState(13);
  const row = EVENTS.find((e) => e.seq === selected) ?? EVENTS[0];
  if (!row) return null;

  return (
    <div className={`mt-12 overflow-hidden bg-fd-card ${panel}`}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-fd-border px-4 py-2.5 text-xs">
        <span className="font-medium text-fd-foreground">await thread.timeline()</span>
        <span className="font-mono text-fd-muted-foreground">{recorded.thread_id.slice(0, 13)}…</span>
        <span className="ml-auto font-mono text-fd-muted-foreground">
          {EVENTS.length} events · append-only
        </span>
      </div>

      <div className="grid md:grid-cols-2">
        <Column
          label="Up to the park"
          aside="Asked over the API. The agent looks up the order, asks to refund it, and the policy hands the decision to a person."
          rows={EVENTS.slice(0, SPLIT)}
          selected={selected}
          onSelect={setSelected}
          className="border-b md:border-r md:border-b-0"
        />
        <Column
          label="After the approval"
          aside="The approval arrives under a new epoch, the run picks up where it parked, the refund goes out, and a second turn follows."
          rows={EVENTS.slice(SPLIT)}
          selected={selected}
          onSelect={setSelected}
          className=""
        />
      </div>

      <div className="border-t border-fd-border bg-fd-background/50 px-4 py-3">
        <p className="text-xs text-fd-muted-foreground">
          <span className="font-mono text-[0.8rem] text-fd-primary">{row.type}</span> · entry {row.seq} of{" "}
          {EVENTS.length}, read from the recording
        </p>
        <dl className="mt-2 gap-x-8 font-mono text-[0.74rem] leading-6 sm:columns-2 xl:columns-3">
          <div className="flex min-w-0 break-inside-avoid gap-x-2">
            <dt className="shrink-0 text-fd-muted-foreground">actor</dt>
            <dd className="min-w-0 break-all text-fd-foreground">{value(row.actor)}</dd>
          </div>
          {Object.entries(row.data).map(([k, v]) => (
            <div key={k} className="flex min-w-0 break-inside-avoid gap-x-2">
              <dt className="shrink-0 text-fd-muted-foreground">data.{k}</dt>
              <dd className="min-w-0 break-all text-fd-foreground">{value(v)}</dd>
            </div>
          ))}
        </dl>
        <p className="mt-2.5 text-xs text-fd-muted-foreground">
          Every value on this page comes from{" "}
          <span className="font-mono text-[0.75rem]">components/home/recorded-run.json</span>, the timeline()
          output of one real run, committed in this repo. Values over {MAX} characters are cut with an
          ellipsis; nothing else is changed.
        </p>
      </div>

      <ul className="grid gap-px border-t border-fd-border bg-fd-border sm:grid-cols-2 lg:grid-cols-4">
        {USES.map((u) => (
          <li key={u.call} className="bg-fd-card px-4 py-3.5">
            <p className="font-mono text-[0.78rem] text-fd-primary">{u.call}</p>
            <p className="mt-1 text-xs leading-5 text-fd-muted-foreground">{u.body}</p>
          </li>
        ))}
      </ul>
    </div>
  );
}
