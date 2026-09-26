import { type A2aFault, fault } from "@threads/a2a/protocol";

// The operations we deliberately do not serve. Each answers by name rather than as an unknown
// method, because the card says so: a client that reads `pushNotifications: false` and then calls a
// config operation anyway deserves the specific refusal, not a 404 that looks like a wrong path.

/** Push notifications are not supported, so every config operation says exactly that. */
const PUSH_CONFIG: readonly string[] = [
  "CreateTaskPushNotificationConfig",
  "GetTaskPushNotificationConfig",
  "ListTaskPushNotificationConfigs",
  "DeleteTaskPushNotificationConfig",
];

const EXTENDED_CARD = "GetExtendedAgentCard";

/** The refusal an operation we do not serve earns, or undefined when it is not one of them. */
export function cutOperation(method: string): A2aFault | undefined {
  if (PUSH_CONFIG.includes(method))
    return fault(
      "PushNotificationNotSupportedError",
      "this agent does not support push notifications; follow the task with SubscribeToTask or GetTask",
    );
  return method === EXTENDED_CARD
    ? fault(
        "UnsupportedOperationError",
        "this agent serves no extended card; its public card is at /.well-known/agent-card.json",
      )
    : undefined;
}

/**
 * The HTTP+JSON paths of those operations, so they answer the same way in both bindings. Matched
 * before the served table, since none of these paths is one of ours.
 */
export function cutPath(path: string, verb: string): A2aFault | undefined {
  if (path === "/extendedAgentCard")
    return cutOperation(verb === "GET" ? EXTENDED_CARD : "");
  return /^\/tasks\/[^/]+\/pushNotificationConfigs(\/[^/]+)?$/.test(path)
    ? cutOperation("GetTaskPushNotificationConfig")
    : undefined;
}
