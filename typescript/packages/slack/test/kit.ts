import { type ChannelAdapter, secret } from "@threads/core/adapter";
import { type SlackOptions, slack } from "../src";

export const SIGNING = "test-signing-secret-value";
export const TOKEN = "xoxb-test-bot-token-value";
process.env["SLACK_TEST_SIGNING"] = SIGNING;
process.env["SLACK_TEST_BOT"] = TOKEN;

export const NOW_S = 1_790_000_000;
export const CHALLENGE = "0192b000-0000-7000-8000-00000000000a";

export function adapter(extra: Partial<SlackOptions> = {}): ChannelAdapter {
  return slack({
    agent: "support",
    signingSecret: secret("SLACK_TEST_SIGNING"),
    botToken: secret("SLACK_TEST_BOT"),
    botUserId: "UBOT",
    now: () => NOW_S * 1000,
    ...extra,
  });
}
