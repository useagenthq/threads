"""`threadsai.host` (spec/api.json package `host`): channels, schedules, the typed HTTP API and
the CLI's server. Serving over HTTP needs the `host` extra (Starlette); the library methods
(`start_run`, `subscribe`, the webhook intake) need nothing beyond core."""

from threadsai.host.a2a.config import A2aExposure, A2aOptions, default_budget
from threadsai.host.app import Authenticate, Host, host
from threadsai.host.channel import (
    Challenged,
    ChannelAdapter,
    ChannelCapabilities,
    Control,
    Decision,
    DeliveryError,
    DeliveryOutcome,
    Ignore,
    Inbound,
    LookupCapability,
    Message,
    RawRequest,
    RawResponse,
    Sent,
    VerifiedDelivery,
)
from threadsai.host.members import HostMemberOptions
from threadsai.host.schedules import Schedule
from threadsai.host.stream import Message as SseMessage

__all__ = [
    "A2aExposure",
    "A2aOptions",
    "Authenticate",
    "Challenged",
    "ChannelAdapter",
    "ChannelCapabilities",
    "Control",
    "Decision",
    "DeliveryError",
    "DeliveryOutcome",
    "Host",
    "HostMemberOptions",
    "Ignore",
    "Inbound",
    "LookupCapability",
    "Message",
    "RawRequest",
    "RawResponse",
    "Schedule",
    "Sent",
    "SseMessage",
    "VerifiedDelivery",
    "default_budget",
    "host",
]
