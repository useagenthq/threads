# @threads/daytona

`daytona()`: Daytona sandboxes as a threads `Sandbox`, over Daytona's official generated API clients (`@daytona/api-client`, `@daytona/toolbox-api-client`). What it declares and why is in `src/index.ts`; this file records what the adapter relies on outside its own code.

## Credential isolation (AGENTS invariant 4)

A sandbox gets no env, and no request body or URL carries the API key. Toolbox requests authenticate to Daytona's toolbox proxy with the API key, as Daytona's SDK does. Keeping the key out of the guest therefore relies on Daytona's infrastructure:

- the proxy strips `Authorization` before forwarding, and
- the runner strips its own credential before the request reaches the toolbox daemon in the guest.

Evidence: upstream `daytonaio/daytona@04cc017e45a1b2e9ef65933dd226944101dbe57d`, `apps/proxy/pkg/proxy/auth.go`, `apps/proxy/pkg/proxy/get_sandbox_target.go`, `apps/runner/pkg/api/middlewares/auth.go`. This is published source, **not verified against the hosted deployment**. The live credential canary (`test/live.test.ts`, `THREADS_LIVE=1`) is an outstanding qualification gate. A custom or self-hosted proxy must strip both the same way.

## Exec output is hex-framed

Daytona's log stream splits stdout from stderr with in-band markers (`01 01 01`, `02 02 02`), and its `PrefixWriter` (`libs/common-go/pkg/log/prefix_writer.go`) doesn't escape them, so output containing those bytes can't be told from a marker. Each command therefore runs under a wrapper that hex-encodes stdout and stderr with `od` in the sandbox, and the adapter decodes them back to the exact bytes (`src/framing.ts`).
