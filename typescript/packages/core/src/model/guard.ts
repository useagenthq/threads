import type { Model } from "./protocol";

// The global model-request guard (AGENTS.md, Tests): once blocked, only test-kit models may be
// sent a request, so a test can never reach a real provider. The test preload blocks it.

const testKit = new WeakSet<Model>();
let blocked = false;

/** Marks a test-kit model (scriptedModel) as always allowed. */
export function markTestKit(model: Model): void {
  testKit.add(model);
}

/** Blocks every model that is not test kit, for the rest of the process. */
export function blockRealModels(): void {
  blocked = true;
}

/** Throws before dispatch when the guard is on and the model is real: a test bug, not a value. */
export function assertModelAllowed(model: Model): void {
  if (blocked && !testKit.has(model))
    throw new Error(
      `model request guard: ${model.info.model.provider}/${model.info.model.name} is not a test-kit model`,
    );
}
