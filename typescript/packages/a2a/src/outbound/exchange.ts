import { canonicalize } from "@threads/core/adapter";
import {
  type A2aFault,
  type Answer,
  call,
  fault,
  isFilePart,
  isSettled,
  type Message,
  payloadOf,
  type Sending,
  SendMessageResponse,
  type Task,
  type TaskState,
  textOf,
  type Wire,
} from "../protocol";

// One exchange with a partner: the send that carries the stored bytes, the parse of what came
// back, and the follow that reads a task to a state it cannot leave on its own. Nothing here
// touches the log — the caller decides what to record, because only it knows what is durable.

/** The statuses a model is shown. `working` means the deadline passed with a receipt in hand. */
export type Status =
  | "completed"
  | "needs_input"
  | "working"
  | "failed"
  | "rejected"
  | "canceled";

const STATUS: Readonly<Record<TaskState, Status>> = {
  TASK_STATE_SUBMITTED: "working",
  TASK_STATE_WORKING: "working",
  TASK_STATE_COMPLETED: "completed",
  TASK_STATE_FAILED: "failed",
  TASK_STATE_CANCELED: "canceled",
  TASK_STATE_REJECTED: "rejected",
  TASK_STATE_INPUT_REQUIRED: "needs_input",
  // The partner wants a credential from us, which no model may supply: it is a failure to report.
  TASK_STATE_AUTH_REQUIRED: "failed",
};

/** The follow's backoff: 1s doubling to 30s. Reads are not effects, so they retry freely. */
const FIRST_WAIT = 1000;
const MAX_WAIT = 30_000;

/**
 * A `SendMessage` of bytes that already exist. `params` is empty on purpose: `SendMessage` carries
 * its request in the body in both bindings, so the URL never depends on it, and the body the first
 * attempt stored is sent unchanged (30-a2a decision H30-1).
 */
export function sendStored(
  wire: Wire,
  body: string,
  sending: Sending,
): Promise<Answer> {
  return call(wire, "SendMessage", {}, { ...sending, body });
}

export function getTask(
  wire: Wire,
  taskId: string,
  sending: Sending,
): Promise<Answer> {
  return call(wire, "GetTask", { id: taskId }, sending);
}

/** What a `SendMessage` answered: a task to follow, a final bare message, or the peer's error. */
export type Sent =
  | { readonly kind: "task"; readonly task: Task }
  | { readonly kind: "message"; readonly message: Message }
  | { readonly kind: "fault"; readonly fault: A2aFault };

/** The response body as one of the three answers. A shape we cannot read is a fault of its own. */
export function sent(value: unknown): Sent {
  const parsed = SendMessageResponse.safeParse(value);
  const payload = parsed.success ? payloadOf(parsed.data) : undefined;
  if (payload === undefined)
    return {
      kind: "fault",
      fault: fault(
        "InvalidAgentResponseError",
        "the peer's answer is neither one task nor one message",
      ),
    };
  if ("task" in payload) {
    const cut = cutInTask(payload.task);
    return cut ?? { kind: "task", task: payload.task };
  }
  const cut = cutInParts(payload.message);
  return cut ?? { kind: "message", message: payload.message };
}

/** The cut: text and JSON parts only, in either direction. */
function cutInParts(message: Message): Sent | undefined {
  return message.parts.some(isFilePart)
    ? {
        kind: "fault",
        fault: fault(
          "ContentTypeNotSupportedError",
          "a file part is not supported; text and JSON parts only",
        ),
      }
    : undefined;
}

function cutInTask(task: Task): Sent | undefined {
  const parts = [
    ...(task.status.message?.parts ?? []),
    ...(task.artifacts ?? []).flatMap((a) => a.parts),
  ];
  return parts.some(isFilePart)
    ? {
        kind: "fault",
        fault: fault(
          "ContentTypeNotSupportedError",
          "a file part is not supported; text and JSON parts only",
        ),
      }
    : undefined;
}

/** A task, and every state this exchange observed on the way to it. */
export type Followed = {
  readonly task: Task;
  readonly seen: readonly Task[];
};

/**
 * Reads the task until it is settled or the deadline passes. A read is not an effect: it never
 * appends an `effect_begin` and it is free to retry. Once we hold a task id a slow peer is just a
 * task still working, so the deadline stops **waiting**, never re-sends.
 */
export async function follow(
  wire: Wire,
  first: Task,
  sending: Sending,
  deadline: number,
  now: () => number = Date.now,
  sleep: (ms: number) => Promise<void> = wait,
): Promise<Followed> {
  const seen: Task[] = [first];
  let task = first;
  let backoff = FIRST_WAIT;
  while (!isSettled(task.status.state)) {
    const left = deadline - now();
    if (left <= 0) break;
    await sleep(Math.min(backoff, left));
    backoff = Math.min(backoff * 2, MAX_WAIT);
    if (now() >= deadline) break;
    const answer = await getTask(wire, task.id, sending);
    if (answer.kind !== "ok") break;
    const read = sent(answer.value);
    if (read.kind !== "task") break;
    task = read.task;
    seen.push(task);
  }
  return { task, seen };
}

function wait(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * What the model sees: canonical JSON, so the same observation renders the same bytes in both
 * languages. A remote's text reaches the model as a tool result, never as an instruction.
 */
export function preview(status: Status, taskId: string, text: string): string {
  const shown = canonicalize({ status, task_id: taskId, text });
  if (!shown.ok) throw new Error("a preview of strings is canonical JSON");
  return shown.value;
}

/** A task's status: what to show, and whether the model should read it as an error. */
export function statusOf(state: TaskState): Status {
  return STATUS[state];
}

/** The text a task carries: its status message, else its artifacts. */
export function taskText(task: Task): string {
  const status = textOf(task.status.message?.parts ?? []);
  if (status !== "") return status;
  return (task.artifacts ?? []).map((a) => textOf(a.parts)).join("\n");
}
