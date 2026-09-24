import { expect, test } from "bun:test";
import { LiveHub } from "../../src/ui/hub";
import { LiveListener } from "../../src/ui/listener";

// The live hub is keyed by tenant and thread: an imported thread can carry any id, so the same
// id in another tenant must never reach this tenant's connections.

test("a delta on another tenant's thread of the same id never reaches a listener", () => {
  const hub = new LiveHub();
  const mine = new LiveListener(hub, "acme", "t-1");
  hub.delta("other", "t-1", { requestId: "r", part: 0, text: "theirs" });
  hub.delta("acme", "t-1", { requestId: "r", part: 0, text: "mine" });
  expect(mine.take().map((d) => d.text)).toEqual(["mine"]);
  const late = new LiveListener(hub, "other", "t-1");
  expect([...(late.missed ?? [])]).toEqual(["r:0"]);
  mine.stop();
  late.stop();
});
