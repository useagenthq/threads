// Secret redaction (spec/schema/README.md "Secret redaction", C5). Every credential the host
// resolves is registered; no recorded content holds one. Event data is redacted where the writer
// stores it; text beside events is redacted where it is written; bytes that must stay
// byte-exact are refused (`containsSecret`).

export {
  containsSecret,
  redactBytes,
  redactingSink,
  SecretInProviderOutput,
} from "./bytes";
export { forgetSecrets, register } from "./registry";
export { redactSecrets, redactStream, redactStrings } from "./text";
