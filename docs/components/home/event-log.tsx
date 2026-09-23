// The timeline of the sample run, as thread.timeline() returned it (recorded on a scripted model).
const EVENTS = [
  { seq: 1, type: "thread_started", note: "a new thread" },
  { seq: 2, type: "user_input", note: "What is the weather in Paris?" },
  { seq: 3, type: "model_request", note: "the exact request sent" },
  { seq: 4, type: "model_response", note: "calls get_weather" },
  { seq: 5, type: "tool_call", note: 'get_weather {"city": "Paris"}' },
  { seq: 6, type: "permission_decision", note: "read_only: no approval needed" },
  { seq: 7, type: "tool_result", note: "It is sunny in Paris." },
  { seq: 8, type: "model_request", note: "the exact request sent" },
  { seq: 9, type: "model_response", note: "It is sunny in Paris." },
  { seq: 10, type: "turn_completed", note: "completed" },
] as const;

export function EventLog() {
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-fd-border px-4 py-2.5 text-xs text-fd-muted-foreground">
        <span className="font-medium text-fd-foreground">thread.timeline()</span>
        <span className="font-mono">append-only</span>
      </div>
      <ol className="flex-1 space-y-0.5 p-2 font-mono text-[0.78rem] leading-5">
        {EVENTS.map((e) => (
          <li
            key={e.seq}
            className="grid grid-cols-[1.75rem_minmax(0,1fr)] gap-x-2 rounded-md px-2 py-1.5 hover:bg-fd-accent/50"
          >
            <span className="text-right text-fd-muted-foreground tabular-nums">{e.seq}</span>
            <span className="min-w-0">
              <span className="text-fd-primary">{e.type}</span>
              <span className="block truncate text-fd-muted-foreground">{e.note}</span>
            </span>
          </li>
        ))}
      </ol>
    </div>
  );
}
