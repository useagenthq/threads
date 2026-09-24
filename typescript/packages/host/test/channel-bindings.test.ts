import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import {
  type ChannelAdapter,
  ConfigError,
  secret,
  sqlite,
} from "@threads/core";
import { github } from "@threads/github";
import { slack } from "@threads/slack";
import { whatsapp } from "@threads/whatsapp";
import { host } from "../src";
import { mailer } from "./kit";

// host.ready() checks each channel factory's configuration: its agent must be a host agent
// (invalid_config) and every secret it names must resolve (missing_secret).

const SECRETS = ["CH_BIND_ONE", "CH_BIND_TWO", "CH_BIND_THREE"] as const;

const CHANNELS: Readonly<Record<string, (agent: string) => ChannelAdapter>> = {
  slack: (agent) =>
    slack({
      agent,
      signingSecret: secret("CH_BIND_ONE"),
      botToken: secret("CH_BIND_TWO"),
    }),
  github: (agent) =>
    github({
      agent,
      webhookSecret: secret("CH_BIND_ONE"),
      token: secret("CH_BIND_TWO"),
    }),
  whatsapp: (agent) =>
    whatsapp({
      agent,
      appSecret: secret("CH_BIND_ONE"),
      accessToken: secret("CH_BIND_TWO"),
      verifyToken: secret("CH_BIND_THREE"),
    }),
};

async function refusal(
  name: string,
  channel: ChannelAdapter,
): Promise<ConfigError> {
  const served = host({
    store: sqlite(":memory:"),
    agents: { support: mailer({ responses: [] }) },
    channels: { [name]: channel },
  });
  try {
    await served.ready();
  } catch (error) {
    if (error instanceof ConfigError) return error;
    throw error;
  } finally {
    await served.stop();
  }
  throw new Error(`${name}: ready() accepted the channel`);
}

const saved = new Map<string, string | undefined>();

beforeEach(() => {
  for (const name of SECRETS) {
    saved.set(name, process.env[name]);
    process.env[name] = `${name.toLowerCase()}-value`;
  }
});

afterEach(() => {
  for (const [name, value] of saved) {
    if (value === undefined) delete process.env[name];
    else process.env[name] = value;
  }
  saved.clear();
});

describe("channel bindings at ready()", () => {
  for (const [name, make] of Object.entries(CHANNELS)) {
    test(`${name}: an agent that isn't a host agent is invalid_config`, async () => {
      const refused = await refusal(name, make("sales"));
      expect(refused.code).toBe("invalid_config");
      expect(refused.message).toContain(name);
      expect(refused.message).toContain("sales");
    });

    test(`${name}: an unset secret is missing_secret`, async () => {
      delete process.env["CH_BIND_TWO"];
      expect((await refusal(name, make("support"))).code).toBe(
        "missing_secret",
      );
    });
  }
});
