import { describe, expect, test } from "bun:test";
import { call } from "../src/protocol/client";
import { errorInfo } from "../src/protocol/errors";
import { answering, httpErr, json, REST, RPC, rpcErr, sending } from "./kit";

// `google.rpc.ErrorInfo`: MUST for the HTTP+JSON binding, SHOULD for JSON-RPC. The asymmetry is the
// point — an HTTP error body without one we recognise is a response we cannot read, while a JSON-RPC
// error's ErrorInfo is checked only when it is there. spec/schema/a2a/README.md records it.

describe("the ErrorInfo the bindings ask for", () => {
  test("an HTTP+JSON error with no ErrorInfo is a response we cannot read", async () => {
    const transport = answering(() =>
      json({ code: -32001, message: "gone" }, 404),
    );
    const answer = await call(
      REST,
      "GetTask",
      { id: "task-1" },
      sending(transport),
    );
    expect(answer.kind).toBe("fault");
    if (answer.kind !== "fault") throw new Error("unreachable");
    expect(answer.fault.name).toBe("InvalidAgentResponseError");
    expect(answer.fault.message).toContain("ErrorInfo");
  });

  test("an HTTP+JSON error whose ErrorInfo names the error is that error", async () => {
    const transport = httpErr("TaskNotFoundError", 404);
    const answer = await call(
      REST,
      "GetTask",
      { id: "task-1" },
      sending(transport),
    );
    expect(answer.kind).toBe("fault");
    if (answer.kind !== "fault") throw new Error("unreachable");
    expect(answer.fault.name).toBe("TaskNotFoundError");
  });

  test("an ErrorInfo reason that disagrees with the status is refused", async () => {
    // TASK_NOT_FOUND is HTTP 404 in the pinned table, so a 400 carrying it is a peer we cannot read.
    const transport = httpErr("TaskNotFoundError", 400);
    const answer = await call(
      REST,
      "GetTask",
      { id: "task-1" },
      sending(transport),
    );
    expect(answer.kind).toBe("fault");
    if (answer.kind !== "fault") throw new Error("unreachable");
    expect(answer.fault.name).toBe("InvalidAgentResponseError");
  });

  test("a reason outside the table is refused rather than guessed from the status", async () => {
    const transport = answering(() =>
      json(
        {
          code: -32001,
          message: "gone",
          details: [{ reason: "SOMETHING_ELSE", domain: "partner.example" }],
        },
        404,
      ),
    );
    const answer = await call(
      REST,
      "GetTask",
      { id: "task-1" },
      sending(transport),
    );
    expect(answer.kind).toBe("fault");
    if (answer.kind !== "fault") throw new Error("unreachable");
    expect(answer.fault.name).toBe("InvalidAgentResponseError");
    expect(answer.fault.message).toContain("SOMETHING_ELSE");
  });

  test("a JSON-RPC error needs no ErrorInfo: the binding only says it SHOULD carry one", async () => {
    const transport = rpcErr({ code: -32002, message: "already done" });
    const answer = await call(
      RPC,
      "CancelTask",
      { id: "task-1" },
      sending(transport),
    );
    expect(answer.kind).toBe("fault");
    if (answer.kind !== "fault") throw new Error("unreachable");
    expect(answer.fault.name).toBe("TaskNotCancelableError");
  });

  test("a JSON-RPC ErrorInfo that disagrees with the code is refused", async () => {
    const transport = rpcErr({
      code: -32002,
      message: "already done",
      data: errorInfo("TaskNotFoundError"),
    });
    const answer = await call(
      RPC,
      "CancelTask",
      { id: "task-1" },
      sending(transport),
    );
    expect(answer.kind).toBe("fault");
    if (answer.kind !== "fault") throw new Error("unreachable");
    expect(answer.fault.name).toBe("InvalidAgentResponseError");
  });

  test("a JSON-RPC ErrorInfo that agrees with the code is the error both name", async () => {
    const transport = rpcErr({
      code: -32002,
      message: "already done",
      data: errorInfo("TaskNotCancelableError"),
    });
    const answer = await call(
      RPC,
      "CancelTask",
      { id: "task-1" },
      sending(transport),
    );
    expect(answer.kind).toBe("fault");
    if (answer.kind !== "fault") throw new Error("unreachable");
    expect(answer.fault.name).toBe("TaskNotCancelableError");
  });
});
