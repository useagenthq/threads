import { afterEach } from "bun:test";
import { blockRealModels } from "../src/model";
import { forgetSecrets } from "../src/redact";

// The global model-request guard for every test process (bunfig.toml preload).
blockRealModels();

// Resolved secrets are process-wide: each test starts with none, so one test's short fake value
// is never redacted in another's.
afterEach(forgetSecrets);
