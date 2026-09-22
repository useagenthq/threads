import type { ErrorCode } from "../log";

/** A typed log failure. Runners compare `code` and `seq`, never `message`. */
export type LogError = {
  readonly code: ErrorCode;
  readonly message: string;
  readonly seq?: number;
};

export function logError(
  code: ErrorCode,
  message: string,
  seq?: number,
): LogError {
  return seq === undefined ? { code, message } : { code, message, seq };
}
