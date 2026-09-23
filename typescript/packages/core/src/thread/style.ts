import type { KnownEvent, Principal } from "../log";
import { err, ok, type Result } from "../result";
import type { Writer } from "../store";
import type { ControlError, Plan } from "./control";

// The two controls that land only between turns (spec/api.json Thread.compact and
// setOutputStyle); control() with `idle` refuses them while a turn is open.

type Planned = (
  events: readonly KnownEvent[],
  writer: Writer,
) => Result<Plan, ControlError>;

/** compact: compaction_requested, which the thread's next run carries out first. */
export function compact(
  principal: Principal,
  instructions: string | undefined,
): Planned {
  return (_events, writer) => {
    if (instructions === "")
      return err({
        code: "invalid_request",
        message: "instructions must be non-empty text, or omitted",
      });
    const { fold } = writer.chain;
    if (fold.compactionRequest !== undefined)
      return err({
        code: "invalid_transition",
        message: "a compaction is already requested",
      });
    if (fold.firstInput === undefined)
      return err({
        code: "invalid_transition",
        message: "nothing to compact yet",
      });
    return ok({
      record: {
        type: "compaction_requested",
        type_version: 1,
        critical: true,
        actor: { kind: "user", principal },
        data: instructions === undefined ? {} : { instructions },
      },
    });
  };
}

/** setOutputStyle: the pinned style's text as a trusted instruction after the prefix. */
export function setOutputStyle(name: string, principal: Principal): Planned {
  return (_events, writer) => {
    const styles = writer.chain.fold.policy?.output_styles ?? {};
    const text = Object.hasOwn(styles, name) ? styles[name] : undefined;
    if (text === undefined) {
      const names = Object.keys(styles);
      return err({
        code: "not_found",
        message: `no output style ${name}; the agent defines ${
          names.length === 0 ? "none" : names.join(", ")
        }`,
      });
    }
    return ok({
      record: {
        type: "injected",
        type_version: 1,
        critical: true,
        actor: { kind: "user", principal },
        data: {
          source: "output_style",
          trust: "trusted_instruction",
          origin: { id: name },
          text,
        },
      },
    });
  };
}
