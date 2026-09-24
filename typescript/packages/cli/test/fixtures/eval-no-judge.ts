import type { Agent } from "@threads/core";
import { support } from "./eval-agents";

// An `--agent` module with agents but no judge: --live needs one.

const agents: readonly Agent[] = [support()];
export default agents;
