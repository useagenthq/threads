import { blockRealModels } from "../src/model";

// The global model-request guard for every test process (bunfig.toml preload).
blockRealModels();
