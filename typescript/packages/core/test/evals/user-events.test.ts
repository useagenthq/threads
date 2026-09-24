import { expect, test } from "bun:test";
import { USER_EVENTS } from "../../src/evals/kinds";
import { ACTOR_KINDS, EVENT_TYPES, INJECTED_SOURCES } from "../../src/log";

// USER_EVENTS is exhaustive (spec lane 22, A.2): every event type and every injected source is
// classified once, so a lane that adds an extension append API or a new injected source fails
// here until it says whether the offline rerun can script it.

test("every event type is scriptable or the framework's own, never both", () => {
  const { scriptable, framework } = USER_EVENTS;
  const classified = [...scriptable.events, ...framework.events];
  expect(classified.toSorted()).toEqual([...EVENT_TYPES].toSorted());
  expect(new Set(classified).size).toBe(classified.length);
});

test("every injected source is scriptable, the framework's or a child thread's, once", () => {
  const sources = [
    ...USER_EVENTS.scriptable.sources,
    ...USER_EVENTS.framework.sources,
    ...USER_EVENTS.child_thread.sources,
  ];
  expect(sources.toSorted()).toEqual([...INJECTED_SOURCES].toSorted());
  expect(new Set(sources).size).toBe(sources.length);
});

test("no actor kind lets user code append events of its own", () => {
  expect(ACTOR_KINDS).not.toContain("extension");
  expect(USER_EVENTS.scriptable.events).toEqual(["hook_decision", "injected"]);
});
