"""`from threads.docker import docker` (extra `docker`)."""

from threads.adapters.sandboxes.docker import DockerSandbox, docker

__all__ = ["DockerSandbox", "docker"]
