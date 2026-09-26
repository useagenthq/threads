import { anthropic } from "@threads/anthropic";
import {
  type Agent,
  type AgentOptions,
  agent,
  localMemory,
  type TeamAgent,
} from "@threads/core";
import { docker } from "@threads/docker";

// codingAgent() (spec/api.json): agent() with five options already chosen. It has no runtime
// concept of its own and no second code path, so a thread it starts and a thread from the
// equivalent agent() call pin the same config_hash.

/**
 * The instructions `codingAgent()` pins when you pass none. The bytes are
 * spec/conformance/vectors/coding-instructions.json, which the Python preset pins too.
 *
 * Extend it (`${CODING_INSTRUCTIONS}\n\nThe repo is acme/app.`) rather than replace it: the
 * baseline commit and the closing `git diff` are how the edits leave the sandbox.
 */
export const CODING_INSTRUCTIONS: string =
  "You are a software engineer working in a sandbox. /workspace is a copy of the user's files; your edits stay in the sandbox. Before your first edit, record a baseline in /workspace: `git init -q; git add -A && git -c user.name=threads -c user.email=threads@localhost commit -q --no-verify --allow-empty -m threads-baseline && git tag -f threads-baseline`. Explore before you change anything: read the relevant files and run the existing tests. Plan multi-step work with todo_write and keep it current. Make the smallest change that solves the task, then verify it by running the tests or the program. Save to memory only when the user asks you to. End your answer with what you changed, what you verified and anything you could not do, followed by the output of `git add -A && git add -A --renormalize && git diff --cached --binary threads-baseline`.";

/**
 * `agent()`'s options, every one optional. A key you pass replaces the preset's value for that
 * key, whole; `memory: undefined` and `sandbox: undefined` remove them.
 */
export type CodingAgentOptions<Deps = undefined, Output = string> = Partial<
  AgentOptions<Deps, Output>
>;

type Team<Deps, Output> = NonNullable<AgentOptions<Deps, Output>["team"]>;
type Schema<Deps, Output> = NonNullable<AgentOptions<Deps, Output>["output"]>;

/** Claude Sonnet 5, keyed from ANTHROPIC_API_KEY when the run is set up. */
const MODEL = "claude-sonnet-5";
/**
 * Edits and commands run inside the Docker sandbox, which isolates them (no network, no bind
 * mounts, every capability dropped, uid 1000, a read-only root), so `bash(*)` is a statement
 * about the boundary, not about trust. Memory writes, host-side tools and the protected paths
 * still ask, and the .threads guard and deny rules are still consulted first.
 */
const PERMISSIONS = {
  mode: "accept_edits",
  allow: ["bash(*)"],
} satisfies AgentOptions<never, never>["permissions"];

/** The preset's five keys, under whatever the caller passed. */
function withDefaults<Deps, Output>(
  o: CodingAgentOptions<Deps, Output>,
): AgentOptions<Deps, Output> {
  return {
    ...o,
    model: o.model ?? anthropic(MODEL),
    instructions: o.instructions ?? CODING_INSTRUCTIONS,
    permissions: o.permissions ?? PERMISSIONS,
    memory: "memory" in o ? o.memory : localMemory(),
    sandbox: "sandbox" in o ? o.sandbox : docker({ cpus: 2, memoryMb: 4096 }),
  };
}

export function codingAgent<Deps = undefined>(
  overrides: CodingAgentOptions<Deps, string> & {
    readonly output?: undefined;
    readonly team: Team<Deps, string>;
  },
): TeamAgent<Deps, string>;
export function codingAgent<Deps, Output>(
  overrides: CodingAgentOptions<Deps, Output> & {
    readonly output: Schema<Deps, Output>;
    readonly team: Team<Deps, Output>;
  },
): TeamAgent<Deps, Output>;
export function codingAgent<Deps = undefined>(
  overrides?: CodingAgentOptions<Deps, string> & {
    readonly output?: undefined;
  },
): Agent<Deps, string>;
export function codingAgent<Deps, Output>(
  overrides: CodingAgentOptions<Deps, Output> & {
    readonly output: Schema<Deps, Output>;
  },
): Agent<Deps, Output>;
export function codingAgent<Deps, Output>(
  overrides: CodingAgentOptions<Deps, Output> = {},
):
  | Agent<Deps, Output>
  | Agent<Deps, string>
  | TeamAgent<Deps, Output>
  | TeamAgent<Deps, string> {
  // As in agent() itself: output and team are taken out, so each call carries exactly the key
  // that selects the overload the caller's own overload promised.
  const { output, team, ...rest } = withDefaults(overrides);
  if (output === undefined) {
    return team === undefined ? agent(rest) : agent({ ...rest, team });
  }
  return team === undefined
    ? agent({ ...rest, output })
    : agent({ ...rest, output, team });
}
