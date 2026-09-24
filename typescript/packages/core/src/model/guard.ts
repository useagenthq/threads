import type { Model } from "./protocol";

// The global model-request guard (AGENTS.md, Tests): once blocked, only test-kit models may be
// sent a request, so a test can never reach a real provider. The test preload blocks it.

const testKit = new WeakSet<Model>();
let blocked = false;

/**
 * Raised by the test model-request guard before dispatch. A subclass of Error, so existing
 * catch blocks behave the same; the eval runner catches exactly this type and stops.
 */
export class ModelBlockedError extends Error {
  /** provider/name of the blocked model. */
  readonly model: string;

  constructor(model: string) {
    super(`model request guard: ${model} is not a test-kit model`);
    this.name = "ModelBlockedError";
    this.model = model;
  }
}

/** Marks a test-kit model (scriptedModel) as always allowed. */
export function markTestKit(model: Model): void {
  testKit.add(model);
}

export function isTestKit(model: Model): boolean {
  return testKit.has(model);
}

/** Blocks every model that is not test kit, for the rest of the process. */
export function blockRealModels(): void {
  blocked = true;
}

/** Throws before dispatch when the guard is on and the model is real: a test bug, not a value. */
export function assertModelAllowed(model: Model): void {
  const { provider, name } = model.info.model;
  if (blocked && !testKit.has(model))
    throw new ModelBlockedError(`${provider}/${name}`);
}
