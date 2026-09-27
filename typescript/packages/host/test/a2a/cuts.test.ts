import { afterEach, expect, test } from "bun:test";
import { AgentCard } from "@threadsai/a2a/protocol";
import { alice } from "../kit";
import { talker } from "./agents";
import { faultName, message, serve, stopAll } from "./kit";

// The cuts the card declares. Each one answers by its own name in both bindings, so a client that
// ignored the card learns exactly what is missing rather than getting a 404 that reads as a typo.

afterEach(stopAll);

const ONE = { support: { description: "Support." } };

const up = () =>
  serve({ agents: { support: talker("hi") }, a2a: { expose: ONE } });

test("the card declares both cuts", async () => {
  const on = await up();
  const card = AgentCard.parse(
    await (
      await on.raw("GET", "/a2a/support/.well-known/agent-card.json")
    ).json(),
  );
  expect(card.capabilities.pushNotifications).toBe(false);
  expect(card.capabilities.extendedAgentCard).toBe(false);
});

test("every push-notification config operation is PushNotificationNotSupportedError", async () => {
  const on = await up();
  for (const method of [
    "CreateTaskPushNotificationConfig",
    "GetTaskPushNotificationConfig",
    "ListTaskPushNotificationConfigs",
    "DeleteTaskPushNotificationConfig",
  ]) {
    const response = await on.rpc(method, { taskId: "t" }, { as: alice });
    expect(await faultName(response)).toBe("PushNotificationNotSupportedError");
  }
});

test("the push-notification config paths answer the same way over HTTP+JSON", async () => {
  const on = await up();
  for (const path of [
    "/tasks/t1/pushNotificationConfigs",
    "/tasks/t1/pushNotificationConfigs/c1",
  ]) {
    const response = await on.http("GET", path, { as: alice });
    expect(await faultName(response)).toBe("PushNotificationNotSupportedError");
  }
});

test("a send that asks for push notifications is refused by name", async () => {
  const on = await up();
  const response = await on.rpc(
    "SendMessage",
    {
      ...(message("m1", "hi") as object),
      configuration: { taskPushNotificationConfig: { url: "https://x" } },
    },
    { as: alice },
  );
  expect(await faultName(response)).toBe("PushNotificationNotSupportedError");
});

test("GetExtendedAgentCard is UnsupportedOperationError in both bindings", async () => {
  const on = await up();
  expect(
    await faultName(await on.rpc("GetExtendedAgentCard", {}, { as: alice })),
  ).toBe("UnsupportedOperationError");
  expect(
    await faultName(await on.http("GET", "/extendedAgentCard", { as: alice })),
  ).toBe("UnsupportedOperationError");
});

test("a method that is neither served nor a named cut is still MethodNotFoundError", async () => {
  const on = await up();
  expect(await faultName(await on.rpc("Teleport", {}, { as: alice }))).toBe(
    "MethodNotFoundError",
  );
});
