#!/usr/bin/env bun
import { run } from "./cli";

const code = await run(process.argv.slice(2), {
  out: (text) => process.stdout.write(text),
  bytes: (data) => process.stdout.write(data),
  err: (text) => process.stderr.write(text),
});
if (code !== 0) process.exit(code);
