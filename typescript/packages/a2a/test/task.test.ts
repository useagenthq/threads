import { describe, expect, test } from "bun:test";
import {
  Artifact,
  Message,
  Part,
  Role,
  TaskStatus,
  textOf,
} from "../src/protocol/task";

// The A2A data model under the protobuf JSON mapping. Every one of these is a rule the proto states
// and JSON Schema can express, so it is enforced where a peer's bytes arrive rather than trusted:
// both languages read the one exported schema, so both accept exactly the same values.

const TEXT = { text: "hi" };

describe("a Part is the proto's oneof, so exactly one content member", () => {
  test("one member parses, whichever it is", () => {
    for (const part of [
      { text: "hi" },
      { raw: "AAA" },
      { url: "https://partner.example/f.pdf" },
      { data: { any: "json" } },
    ])
      expect(Part.safeParse(part).success).toBe(true);
  });

  test("the shared fields ride along with any member", () => {
    expect(
      Part.safeParse({
        text: "hi",
        filename: "a.txt",
        mediaType: "text/plain",
        metadata: { k: "v" },
      }).success,
    ).toBe(true);
  });

  test("no member at all is refused: a part carrying nothing is not a part", () => {
    // It used to parse, and then read back as empty text through textOf.
    expect(Part.safeParse({}).success).toBe(false);
  });

  test("two members at once are refused: a oneof is one member", () => {
    expect(Part.safeParse({ text: "hi", raw: "AAA" }).success).toBe(false);
    expect(
      Part.safeParse({ url: "https://partner.example/f", data: 1 }).success,
    ).toBe(false);
  });

  test("bytes are base64, as the JSON mapping says", () => {
    for (const raw of ["", "AAA", "AA==", "AAAA", "a-_9"])
      expect(Part.safeParse({ raw }).success).toBe(true);
    for (const raw of ["!!!not base64!!!", "A", "AA=A", "AAAAA"])
      expect(Part.safeParse({ raw }).success).toBe(false);
  });

  test("a part with no text reads as no text, and never as empty text", () => {
    const part = Part.parse({ data: { k: 1 } });
    expect(textOf([part])).toBe("");
  });
});

describe("parts is REQUIRED, which for a repeated field means at least one", () => {
  test("a Message with no parts is refused", () => {
    expect(
      Message.safeParse({ messageId: "m1", role: "ROLE_USER", parts: [] })
        .success,
    ).toBe(false);
  });

  test("a Message with a part is accepted", () => {
    expect(
      Message.safeParse({ messageId: "m1", role: "ROLE_USER", parts: [TEXT] })
        .success,
    ).toBe(true);
  });

  test("an Artifact with no parts is refused: the proto says it must contain at least one", () => {
    expect(Artifact.safeParse({ artifactId: "a1", parts: [] }).success).toBe(
      false,
    );
    expect(
      Artifact.safeParse({ artifactId: "a1", parts: [TEXT] }).success,
    ).toBe(true);
  });
});

describe("a timestamp is RFC 3339, checked rather than described", () => {
  const status = (timestamp: string) =>
    TaskStatus.safeParse({ state: "TASK_STATE_WORKING", timestamp }).success;

  test("the forms google.protobuf.Timestamp writes are accepted", () => {
    for (const stamp of [
      "2023-10-27T10:00:00Z",
      "2023-10-27T10:00:00.021Z",
      "2023-10-27T10:00:00+01:00",
      "2023-10-27t10:00:00z",
    ])
      expect(status(stamp)).toBe(true);
  });

  test("anything that is not a timestamp is refused", () => {
    for (const stamp of [
      "yesterday",
      "2023-10-27",
      "10:00:00Z",
      "2023-10-27T10:00:00",
      "",
    ])
      expect(status(stamp)).toBe(false);
  });

  test("an absent timestamp is still fine: the proto does not require one", () => {
    expect(TaskStatus.safeParse({ state: "TASK_STATE_WORKING" }).success).toBe(
      true,
    );
  });
});

describe("an enum's zero value is never an answer we can act on", () => {
  test("the two real roles are accepted", () => {
    expect(Role.safeParse("ROLE_USER").success).toBe(true);
    expect(Role.safeParse("ROLE_AGENT").success).toBe(true);
  });

  test("ROLE_UNSPECIFIED is refused, for the same reason TASK_STATE_UNSPECIFIED is", () => {
    expect(Role.safeParse("ROLE_UNSPECIFIED").success).toBe(false);
  });
});
