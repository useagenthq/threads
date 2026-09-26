"use client";

import { useState } from "react";

/*
 * One run, as thread.timeline() returns it: every entry is a recorded event plus a fork_point flag.
 *
 * The run is the same one the trace panel below shows as spans, so a reader can see that the log
 * and the spans are one record read twice. Field names and enum values come from the event schema
 * (spec/schema/events.v1.schema.json); nothing here is invented.
 *
 * The two turns sit side by side rather than in one scrolling column: the park, the approval and
 * the fork point are the point of the example, and an inner scrollbar would hide them.
 */

type Row = {
  readonly seq: number;
  readonly type: string;
  readonly note: string;
  readonly detail: readonly (readonly [string, string])[];
  readonly forkPoint?: true;
};

type Phase = {
  readonly label: string;
  readonly aside: string;
  readonly rows: readonly Row[];
  readonly footer?: string;
};

const PHASES: readonly Phase[] = [
  {
    label: "Turn 1",
    aside: "Asked in the API, parked on an approval.",
    rows: [
      {
        seq: 1,
        type: "thread_started",
        note: "support · claude-sonnet-5 · 3 tools",
        detail: [
          ["actor", "host"],
          ["data.agent_name", '"support"'],
          ["data.model", '{ provider: "anthropic", name: "claude-sonnet-5" }'],
          ["data.tools", "[lookup_order, list_refunds, mcp__billing__refund]"],
          ["data.instructions", '"You handle billing questions for…" (312 chars)'],
          ["data.config_hash", "1f3c9ab4…"],
        ],
      },
      {
        seq: 2,
        type: "user_input",
        note: "Refund order A-2291, it arrived broken.",
        detail: [
          ["actor", "user"],
          ["data.source", '"api"'],
          ["data.text", '"Refund order A-2291, it arrived broken."'],
        ],
      },
      {
        seq: 3,
        type: "model_request",
        note: "the exact bytes the model received",
        detail: [
          ["actor", "host"],
          ["data.attempt", "1"],
          ["data.purpose", '"turn"'],
          ["data.request_ref", '{ sha256: "9f2c4e…", bytes: 4812 }'],
          ["data.declared_prefix", '{ sha256: "4be1d0…", bytes: 2190 }'],
        ],
      },
      {
        seq: 4,
        type: "model_response",
        note: "tool_use · 1,284 in / 96 out",
        detail: [
          ["actor", "model"],
          ["data.stop_reason", '"tool_use"'],
          ["data.completeness", '"complete"'],
          ["data.usage", "{ input_tokens: 1284, output_tokens: 96, cache_read_tokens: 2048 }"],
        ],
      },
      {
        seq: 5,
        type: "tool_call",
        note: 'lookup_order {"order_id": "A-2291"}',
        detail: [
          ["actor", "model"],
          ["data.call_id", '"call_5b1"'],
          ["data.name", '"lookup_order"'],
          ["data.input", '{ "order_id": "A-2291" }'],
        ],
      },
      {
        seq: 6,
        type: "permission_decision",
        note: "allow · read_only, no approval",
        detail: [
          ["actor", "host"],
          ["data.call_id", '"call_5b1"'],
          ["data.decision", '"allow"'],
          ["data.source", '"default"'],
        ],
      },
      {
        seq: 7,
        type: "tool_result",
        note: "A-2291 · delivered 3 Sep · $84.00",
        detail: [
          ["actor", "tool"],
          ["data.call_id", '"call_5b1"'],
          ["data.is_error", "false"],
          ["data.origin", '"executed"'],
          ["data.preview", '"A-2291 · delivered 3 Sep · $84.00"'],
        ],
      },
      {
        seq: 8,
        type: "model_response",
        note: "tool_use · asks for the refund",
        detail: [
          ["actor", "model"],
          ["data.stop_reason", '"tool_use"'],
          ["data.usage", "{ input_tokens: 1461, output_tokens: 74 }"],
        ],
      },
      {
        seq: 9,
        type: "tool_call",
        note: "mcp__billing__refund · $84.00",
        detail: [
          ["actor", "model"],
          ["data.call_id", '"call_7g2"'],
          ["data.name", '"mcp__billing__refund"'],
          ["data.input", '{ "order_id": "A-2291", "amount_cents": 8400 }'],
        ],
      },
      {
        seq: 10,
        type: "permission_decision",
        note: "ask · a person has to approve",
        detail: [
          ["actor", "host"],
          ["data.call_id", '"call_7g2"'],
          ["data.decision", '"ask"'],
          ["data.source", '"policy"'],
          ["data.rule_id", '"refunds-need-approval"'],
        ],
      },
      {
        seq: 11,
        type: "approval_requested",
        note: "call_7g2 · expires in 24h",
        detail: [
          ["actor", "host"],
          ["data.call_id", '"call_7g2"'],
          ["data.args_hash", "c0d81f…"],
          ["data.challenge_id", '"0193f2a1-…"'],
        ],
      },
      {
        seq: 12,
        type: "parked",
        note: "awaiting_approval · the process can exit",
        detail: [
          ["actor", "host"],
          ["data.reason", '"awaiting_approval"'],
          ["data.address", '{ kind: "approval", id: "call_7g2" }'],
        ],
      },
    ],
  },
  {
    label: "Turn 2",
    aside: "Six minutes later, approved in Slack. Another process, same log.",
    footer:
      "Entry 20 is a fork point. fork() starts a new branch from exactly this state, sandbox included, and the branch keeps its own events from there.",
    rows: [
      {
        seq: 13,
        type: "approval_granted",
        note: "approver · amrita@acme.com",
        detail: [
          ["actor", 'approver { principal: "amrita@acme.com" }'],
          ["data", "{}"],
        ],
      },
      {
        seq: 14,
        type: "resumed",
        note: "picks up where it parked",
        detail: [
          ["actor", "host"],
          ["data.address", '{ kind: "approval", id: "call_7g2" }'],
          ["data.cause_event_id", '"ev_01JBQ5…"'],
        ],
      },
      {
        seq: 15,
        type: "effect_begin",
        note: "durable before the call goes out",
        detail: [
          ["actor", "host"],
          ["data.call_id", '"call_7g2"'],
          ["data.attempt", "1"],
        ],
      },
      {
        seq: 16,
        type: "effect_commit",
        note: "the provider's receipt, recorded",
        detail: [
          ["actor", "host"],
          ["data.call_id", '"call_7g2"'],
          ["data.provider_receipt", '"re_3PqL8xK2"'],
          ["data.result_ref", '{ sha256: "b7a5c1…", bytes: 318 }'],
        ],
      },
      {
        seq: 17,
        type: "tool_result",
        note: "Refunded $84.00 to the original card.",
        detail: [
          ["actor", "tool"],
          ["data.call_id", '"call_7g2"'],
          ["data.origin", '"materialized_from_commit"'],
          ["data.preview", '"Refunded $84.00 to the original card."'],
        ],
      },
      {
        seq: 18,
        type: "model_response",
        note: "end_turn · writes the answer",
        detail: [
          ["actor", "model"],
          ["data.stop_reason", '"end_turn"'],
          ["data.usage", "{ input_tokens: 1702, output_tokens: 61 }"],
        ],
      },
      {
        seq: 19,
        type: "turn_completed",
        note: "end_turn",
        detail: [
          ["actor", "host"],
          ["data.reason", '"end_turn"'],
        ],
      },
      {
        seq: 20,
        type: "snapshot",
        note: "fork point · branch from here",
        forkPoint: true,
        detail: [
          ["actor", "host"],
          ["data.provider", '"e2b"'],
          ["data.capture_class", '"filesystem"'],
          ["data.manifest_hash", "6e42bb…"],
          ["fork_point", "true"],
        ],
      },
    ],
  },
];

const ROWS: readonly Row[] = PHASES.flatMap((p) => p.rows);

/** What the log is for, in the reader's terms. Each one reads these same events. */
const USES: readonly { readonly call: string; readonly body: string }[] = [
  { call: "timeline()", body: "Read a bad answer back, months later, with no logging added." },
  { call: "fork()", body: "Branch at a fork point and try another prompt from the same state." },
  { call: "run()", body: "Resume after a crash without repeating the refund." },
  { call: "saveCase()", body: "Keep this turn as a test that reruns with no model calls." },
];

function EventRow({ row, active, onSelect }: { row: Row; active: boolean; onSelect: (seq: number) => void }) {
  return (
    <li>
      <button
        type="button"
        onClick={() => onSelect(row.seq)}
        aria-current={active ? "true" : undefined}
        className={`grid w-full grid-cols-[1.4rem_minmax(0,auto)_minmax(0,1fr)] items-baseline gap-x-2 rounded border-l-2 py-[3px] pr-2 pl-1.5 text-left font-mono cursor-pointer text-[0.72rem] leading-5 transition-colors sm:text-[0.76rem] ${
          active ? "border-fd-primary bg-fd-primary/10" : "border-transparent hover:bg-fd-accent/70"
        }`}
      >
        <span className="text-right text-fd-muted-foreground tabular-nums">{row.seq}</span>
        <span className={active ? "text-fd-primary" : "text-fd-foreground"}>{row.type}</span>
        <span className="min-w-0 truncate text-fd-muted-foreground">
          {row.forkPoint ? (
            <span className="mr-1.5 rounded border border-fd-primary/40 px-1 text-[0.6rem] tracking-wide text-fd-primary uppercase">
              fork
            </span>
          ) : null}
          {row.note}
        </span>
      </button>
    </li>
  );
}

export function Timeline() {
  const [selected, setSelected] = useState(12);
  const row = ROWS.find((r) => r.seq === selected) ?? ROWS[0];

  return (
    <div className="mt-12 overflow-hidden rounded-2xl border border-fd-border bg-fd-card">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-fd-border px-4 py-2.5 text-xs">
        <span className="font-medium text-fd-foreground">await thread.timeline()</span>
        <span className="font-mono text-fd-muted-foreground">thr_01JBQ4Z7 · main</span>
        <span className="ml-auto font-mono text-fd-muted-foreground">20 events · append-only</span>
      </div>

      <div className="grid md:grid-cols-2">
        {PHASES.map((phase, i) => (
          <div
            key={phase.label}
            className={`border-fd-border px-2 py-3 ${i === 0 ? "border-b md:border-r md:border-b-0" : ""}`}
          >
            <div className="px-1.5 pb-2">
              <p className="font-mono text-[0.68rem] tracking-widest text-fd-muted-foreground uppercase">
                {phase.label}
              </p>
              <p className="mt-0.5 text-xs leading-5 text-fd-muted-foreground">{phase.aside}</p>
            </div>
            <ol>
              {phase.rows.map((r) => (
                <EventRow key={r.seq} row={r} active={r.seq === selected} onSelect={setSelected} />
              ))}
            </ol>
            {phase.footer ? (
              <p className="mt-3 border-t border-fd-border px-1.5 pt-3 text-xs leading-5 text-fd-muted-foreground">
                {phase.footer}
              </p>
            ) : null}
          </div>
        ))}
      </div>

      <div className="border-t border-fd-border bg-fd-background/50 px-4 py-3">
        <p className="text-xs text-fd-muted-foreground">
          <span className="font-mono text-[0.8rem] text-fd-primary">{row.type}</span> · entry {row.seq} of 20,
          exactly as it is stored
        </p>
        <dl className="mt-2 grid gap-x-8 font-mono text-[0.74rem] leading-6 sm:grid-cols-2 xl:grid-cols-3">
          {row.detail.map(([k, v]) => (
            <div key={k} className="flex min-w-0 gap-x-2">
              <dt className="shrink-0 text-fd-muted-foreground">{k}</dt>
              <dd className="min-w-0 break-words text-fd-foreground">{v}</dd>
            </div>
          ))}
        </dl>
        <p className="mt-2.5 text-xs text-fd-muted-foreground">
          Held as one canonical JSON line: the same bytes in SQLite, in{" "}
          <span className="font-mono text-[0.75rem]">threads export</span> and in the conformance fixtures.
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
