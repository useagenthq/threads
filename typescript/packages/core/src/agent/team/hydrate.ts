import { assertNever } from "../../assert-never";
import type { ArtifactRef, StoredMemberResult } from "../../log";
import type { ArtifactStore } from "../../store/artifacts";
import type { MemberResult } from "../../team/results";
import { StoreCorruptError } from "../errors";

// Hydration (design §2.4): a result API turns a stored result into the public one, reading a
// completed output's {ref} from the content-addressed store, verified by sha256 and length. A ref
// the verified log names is always present in a correct store, so a failure is thrown.

const utf8 = new TextDecoder("utf-8", { fatal: true });

/** The public result of a stored one. Throws StoreCorruptError. */
export function hydrated(
  result: StoredMemberResult,
  artifacts: ArtifactStore,
): MemberResult {
  switch (result.status) {
    case "completed": {
      const { output } = result;
      const text =
        "text" in output ? output.text : readText(artifacts, output.ref);
      return { member: result.member, status: "completed", output: text };
    }
    case "handed_off":
      return {
        member: result.member,
        status: "handed_off",
        toThread: result.to_thread,
      };
    case "failed":
    case "cancelled":
    case "budget_exhausted":
      return result;
    default:
      return assertNever(result);
  }
}

function readText(artifacts: ArtifactStore, ref: ArtifactRef): string {
  const got = artifacts.get(ref.sha256);
  if (!got.ok) {
    const code =
      got.error.code === "artifact_missing"
        ? "artifact_missing"
        : "artifact_corrupt";
    throw new StoreCorruptError(code, ref, got.error.message);
  }
  if (got.value.length !== ref.bytes)
    throw new StoreCorruptError(
      "artifact_corrupt",
      ref,
      `artifact ${ref.sha256} is ${got.value.length} bytes, not ${ref.bytes}`,
    );
  try {
    return utf8.decode(got.value);
  } catch (error) {
    // A verified ref to bytes that aren't text: the ref itself is wrong.
    if (!(error instanceof TypeError)) throw error;
    throw new StoreCorruptError("artifact_corrupt", ref, error.message);
  }
}
