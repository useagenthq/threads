import { type Agent, agent, scriptedModel } from "@threads/core";

// An `--agent` module whose support agent changed its instructions since the cases were saved.

const changed: Agent = agent({
  name: "support",
  instructions: "You answer order and billing questions.",
  model: scriptedModel({ responses: [] }),
});

export default changed;
