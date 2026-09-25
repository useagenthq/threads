import type { z } from "zod";
import type {
  EventId,
  KnownEvent,
  ModelSettings,
  PermissionMode,
  Principal,
} from "../log";
import { err, ok, type Result } from "../result";
import type { Chain } from "../verify";
import {
  type ControlError,
  type Plan,
  resumed,
  type SettingsChange,
} from "./control";

// The cancel, mode and model controls (spec/api.json Thread.cancel, setMode, setModel).

type Mode = z.infer<typeof PermissionMode>;
type Settings = z.infer<typeof ModelSettings>;

/**
 * cancel: the durable barrier. A question or approval the branch waits on is
 * released so the loop can close it; an unsettled effect stays parked.
 */
export function cancel(
  principal: Principal,
): (events: readonly KnownEvent[], chain: Chain) => Result<Plan, ControlError> {
  return (_events, chain) => {
    const waiting = chain.fold.parked.filter(
      (a) => a.kind === "approval" || a.kind === "input",
    );
    return ok({
      record: {
        type: "cancel_requested",
        type_version: 1,
        critical: true,
        actor: { kind: "user", principal },
        data: { scope: "thread" },
      },
      ...(waiting.length === 0
        ? {}
        : { after: (id: EventId) => waiting.map((a) => resumed(a, id)) }),
    });
  };
}

/**
 * A channel's soft stop, recorded as the reserved stop_when_idle event. The reducer gives it no
 * effect yet, so the run goes on; unlike cancel it releases nothing the branch waits on and
 * stops no child.
 */
export function stopWhenIdle(
  principal: Principal,
): (events: readonly KnownEvent[], chain: Chain) => Result<Plan, ControlError> {
  return () =>
    ok({
      record: {
        type: "stop_when_idle",
        type_version: 1,
        critical: true,
        actor: { kind: "user", principal },
        data: { reason: "requested by the thread's principal" },
      },
    });
}

/** setMode: mode_changed from the current mode; rule checks are the log's. */
export function setMode(
  mode: Mode,
  principal: Principal,
): (events: readonly KnownEvent[], chain: Chain) => Result<Plan, ControlError> {
  return (_events, chain) =>
    ok({
      record: {
        type: "mode_changed",
        type_version: 1,
        critical: true,
        actor: { kind: "user", principal },
        data: { from: chain.fold.mode, to: mode },
      },
    });
}

/**
 * setModel: settings_changed{reason: user}. The adapter and default params come from the
 * settings the thread already recorded for that model (its start, a fallback or an earlier
 * change); a model it never recorded is invalid_transition.
 */
export function setModel(
  change: SettingsChange,
  principal: Principal,
): (events: readonly KnownEvent[], chain: Chain) => Result<Plan, ControlError> {
  return (events, chain) => {
    const known = recordedSettings(events, chain).findLast(
      (s) =>
        s.model.provider === change.model.provider &&
        s.model.name === change.model.name,
    );
    if (known === undefined)
      return err({
        code: "invalid_transition",
        message: `${change.model.provider}/${change.model.name} has no recorded adapter`,
      });
    return ok({
      record: {
        type: "settings_changed",
        type_version: 1,
        critical: true,
        actor: { kind: "user", principal },
        data: {
          reason: "user",
          settings: {
            model: change.model,
            adapter: known.adapter,
            model_params: change.model_params ?? known.model_params,
            reasoning_carryover: change.reasoning_carryover ?? "keep",
          },
        },
      },
    });
  };
}

function recordedSettings(
  events: readonly KnownEvent[],
  chain: Chain,
): readonly Settings[] {
  const fallback = chain.fold.policy?.fallback ?? [];
  const recorded = events.flatMap((e): Settings[] => {
    if (e.type === "settings_changed") return [e.data.settings];
    if (e.type !== "thread_started") return [];
    const { model, adapter, model_params } = e.data;
    return [{ model, adapter, model_params, reasoning_carryover: "keep" }];
  });
  return [...fallback, ...recorded];
}
