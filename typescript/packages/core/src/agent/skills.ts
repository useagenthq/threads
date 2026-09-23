import { sha256Hex } from "../hash";
import { Name, ToolSpec } from "../log";
import type { ToolImpl } from "../loop/types";
import { entry } from "../tools/catalog";
import { LoadSkillInput } from "../tools/memory-inputs";
import { ConfigError } from "./errors";
import { jsonSchema } from "./tool";

// Skills: host config, pinned by config_hash at thread start.
// Line 0 lists each name and description; load_skill appends a body from this pinned set only,
// never from the sandbox or repo (F4.3). Compaction restores loaded skills (loop/restore.ts).

/** spec/api.json Skill. */
export type Skill = {
  readonly name: string;
  readonly description: string;
  readonly body: string;
};

/** Config is a boundary: names are unique Names, descriptions one non-empty line. */
export function checkSkills(skills: readonly Skill[]): void {
  const names = skills.map((s) => s.name);
  const bad = skills.find(
    (s) =>
      !Name.safeParse(s.name).success ||
      s.description.trim() === "" ||
      /[\r\n]/.test(s.description),
  );
  if (bad !== undefined)
    throw new ConfigError(
      "invalid_config",
      `skill ${JSON.stringify(bad.name)} needs a lowercase name and a one-line description`,
    );
  const twice = names.find((n, i) => names.indexOf(n) !== i);
  if (twice !== undefined)
    throw new ConfigError("duplicate_name", `two skills are named ${twice}`);
}

/** The listing paragraph of line 0's system text; none without skills. */
export function skillListing(skills: readonly Skill[]): readonly string[] {
  if (skills.length === 0) return [];
  const lines = skills.map((s) => `- ${s.name}: ${s.description}`);
  return [["Skills you can load with load_skill:", ...lines].join("\n")];
}

/** What config_hash pins: the listing plus each body's hash (bodies aren't in line 0). */
export function skillPins(
  skills: readonly Skill[],
): readonly Record<string, string>[] {
  return skills.map((s) => ({
    name: s.name,
    description: s.description,
    sha256: sha256Hex(s.body),
  }));
}

export function skillSpecs(skills: readonly Skill[]): readonly ToolSpec[] {
  if (skills.length === 0) return [];
  const e = entry("load_skill");
  return [
    ToolSpec.parse({
      name: e.name,
      description: e.description,
      input_schema: jsonSchema(e.name, e.input),
      effect_class: "read_only",
    }),
  ];
}

/** load_skill over the pinned set: the body arrives as injected trusted instruction. */
export function skillTools(skills: readonly Skill[]): readonly ToolImpl[] {
  return skillSpecs(skills).map((spec) => ({
    spec,
    input: LoadSkillInput,
    run: async (raw) => {
      const { name } = LoadSkillInput.parse(raw);
      const skill = skills.find((s) => s.name === name);
      if (skill === undefined)
        return {
          kind: "done",
          output: `not_found: no skill named ${name}; only the listed skills exist`,
          isError: true,
        };
      return {
        kind: "done",
        output: `loaded skill ${name}`,
        isError: false,
        inject: [
          {
            source: "skill",
            trust: "trusted_instruction",
            origin: { id: name, version: sha256Hex(skill.body) },
            text: skill.body,
          },
        ],
      };
    },
  }));
}
