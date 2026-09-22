/** Ends every switch over a union: a new variant fails to compile here instead of falling through. */
export function assertNever(value: never): never {
  throw new Error(`unexpected variant: ${JSON.stringify(value)}`);
}
