"""The local confined sandbox adapter: no provider, and no snapshots (lane 16 D3)."""

from threads.adapters.sandboxes.dev.sandbox import DevSandbox, dev_sandbox

__all__ = ["DevSandbox", "dev_sandbox"]
