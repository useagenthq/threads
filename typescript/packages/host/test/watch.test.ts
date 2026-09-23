import { describe, expect, test } from "bun:test";
import { ThreadId } from "@threads/core/host";
import { Watch } from "../src/watch";

const T = ThreadId.parse("01a0cf30-3969-7caf-adb4-abbec478caa6");

describe("the watch on channel threads", () => {
  test("a thread seen settled is dropped", () => {
    const watch = new Watch();
    watch.add("t", T);
    for (const { settled } of watch.entries()) settled();
    expect(watch.has(T)).toBe(false);
  });

  test("an older pass's verdict never drops a thread watched again since", () => {
    const watch = new Watch();
    watch.add("t", T);
    const pass = watch.entries();
    // An item consumed while the pass looked: the thread must be looked at again.
    watch.add("t", T);
    for (const { settled } of pass) settled();
    expect(watch.has(T)).toBe(true);
  });
});
