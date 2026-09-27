import type { Tool } from "threadsai";
import {
  ConfigError,
  jsonSchema,
  liveTransport,
  type ToolContext,
  type ToolImpl,
  type ToolSpec,
} from "threadsai/adapter";
import { z } from "zod";
import type { Remote } from "../a2a";
import type { Sending } from "../protocol";
import { reconcileSend } from "./reconcile";
import { beginSend, runSend } from "./send";
import { runStatus } from "./status";

// remote(...).tools(): a partner's agent as two pinned host tools, so it spreads next to the
// thread's own tools. Two tools rather than one, because a tool's effect class is fixed when it is
// pinned: a read folded into the send tool would carry the send's class and ask for approval to
// send nothing.

/** 16 KiB of text: a message, not a document. A partner's own limits are its own to enforce. */
const MAX_MESSAGE = 16_384;

const SendInput = z
  .strictObject({
    message: z.string().min(1).max(MAX_MESSAGE),
    task_id: z
      .string()
      .min(1)
      .optional()
      .describe("Continue a task that asked this conversation for input."),
  })
  .describe("One message to the remote agent.");

const StatusInput = z
  .strictObject({
    task_id: z.string().min(1).describe("A task this conversation created."),
  })
  .describe("Read a task this conversation already created. Sends no message.");

export type RemoteToolsOptions = {
  /** The tool name the model sees; `<name>_status` is pinned beside it. */
  readonly name: string;
  readonly description: string;
};

export function remoteTools(
  remote: Remote,
  o: RemoteToolsOptions,
): readonly Tool<unknown, unknown, unknown>[] {
  if (!/^[a-z][a-z0-9_]*$/.test(o.name))
    throw new ConfigError(
      "invalid_config",
      `remote tool name ${JSON.stringify(o.name)} must match [a-z][a-z0-9_]*`,
    );
  const send: ToolSpec = {
    name: o.name,
    description: o.description,
    input_schema: jsonSchema(o.name, SendInput),
    // reconcilable, not idempotent: the dedup window comes from a partner's card, which is not
    // known when the tool is pinned, and pinning one would claim dedup we have not seen.
    effect_class: "reconcilable",
  };
  const status: ToolSpec = {
    name: `${o.name}_status`,
    description: `${o.description} This reads the state of a task ${o.name} created and sends nothing. It records each state change it observes in this conversation's log.`,
    input_schema: jsonSchema(`${o.name}_status`, StatusInput),
    effect_class: "read_only",
  };
  return [
    {
      name: send.name,
      spec: () => send,
      bind: (env) => sendTool(remote, send, env.threadId),
    },
    {
      name: status.name,
      spec: () => status,
      bind: () => statusTool(remote, status),
    },
  ];
}

function sendTool(remote: Remote, spec: ToolSpec, threadId: string): ToolImpl {
  return {
    spec,
    input: SendInput,
    begin: async (input, ctx) => {
      const args = SendInput.safeParse(input);
      if (!args.success)
        return { ok: false, error: `invalid arguments for ${spec.name}` };
      return beginSend(
        remote,
        threadId,
        { message: args.data.message, taskId: args.data.task_id },
        ctx,
        sending(remote, ctx),
      );
    },
    run: (_input, ctx) => runSend(remote, ctx, sending(remote, ctx)),
    reconcile: {
      // "Not found" never proves absence: a lookup that finds nothing parks instead of resending.
      finality: "nonfinal",
      lookup: (_input, ctx) => reconcileSend(remote, ctx, sending(remote, ctx)),
    },
  };
}

function statusTool(remote: Remote, spec: ToolSpec): ToolImpl {
  return {
    spec,
    input: StatusInput,
    run: async (input, ctx) => {
      const args = StatusInput.safeParse(input);
      return args.success
        ? runStatus(remote, args.data.task_id, ctx, sending(remote, ctx))
        : {
            kind: "done",
            output: `invalid arguments for ${spec.name}`,
            isError: true,
          };
    },
  };
}

/** The credential is resolved here, at send time, and rides in the header only (invariant 4). */
function sending(remote: Remote, ctx: ToolContext): Sending {
  return {
    transport: remote.transport ?? liveTransport,
    ...(remote.auth === undefined
      ? {}
      : { authorization: `Bearer ${remote.auth.reveal()}` }),
    signal: ctx.signal,
    timeoutMs: remote.timeoutMs,
  };
}
