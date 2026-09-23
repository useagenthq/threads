import { ConfigError, type ProviderSandbox } from "@threads/core/adapter";

// modal(): refused at setup. Modal's official JS SDK (npm `modal`) runs sandboxes, but every
// exec, stdio stream and file operation goes over its task command router: a gRPC channel the
// SDK dials itself, outside the `grpcMiddleware` hook it offers for the control plane. threads
// can't fence that transport at its real send, so the route is never shipped with a weaker
// fence (spec/api.json SandboxContext), and Modal documents no public HTTP API to build on
// instead. When the SDK lets a caller wrap the command router transport, this becomes an
// adapter on the remote kit like e2b() and daytona(); its declarations would then follow
// Modal's documentation: terminate(wait) waits for the sandbox to end, and a
// filesystem snapshot terminates the sandbox, can't run during an exec and restores no
// background process, so snapshots would be declared disruptive to the parent.

export function modal(): ProviderSandbox {
  throw new ConfigError(
    "transport_fence_unsupported",
    "modal: the Modal SDK's command router transport (exec, stdio, files) can't be fenced",
  );
}
