"""Shows: billing as a host member — one long-lived agent per tenant that every thread of that
tenant may ask by name. support has no `team` and starts nothing: the rule that allows `ask`
makes it a caller, and its model gets exactly that one team tool. billing's own pin gets `reply`
and nothing else, because a host team grants nothing and no rule has billing as `from`. The rule's
budget caps each turn a caller's ask opens; everything else is denied and recorded as
message_policy_decided.

Needs: ANTHROPIC_API_KEY, SLACK_SIGNING_SECRET, SLACK_BOT_TOKEN
Run: threadsai dev examples/host_members.py (it exports `app`, as README.md does).
"""

from pydantic import BaseModel, Field

from threadsai import RunContext, agent, secret, sqlite, tool, usd
from threadsai.anthropic import anthropic
from threadsai.host import host
from threadsai.host.members import HostMemberOptions
from threadsai.log import Budget
from threadsai.slack import slack

INVOICES: dict[str, str] = {"INV-1001": "paid", "INV-1002": "overdue"}


class InvoiceInput(BaseModel):
    # A JSON Schema pattern, so the model sees the same rule as the TypeScript twin.
    id: str = Field(pattern=r"^INV-\d{4}$")


async def invoice_status_body(args: InvoiceInput, ctx: RunContext[None]) -> str:
    return INVOICES.get(args.id, "unknown invoice")


invoice_status = tool(
    name="invoice_status",
    description="Look up an invoice's status by id, e.g. INV-1001.",
    input=InvoiceInput,
    runs="host",
    # read_only, so this host member needs no approvers: a shared member's approval would
    # otherwise hold every other conversation's mail until someone answered it.
    effect="read_only",
    execute=invoice_status_body,
)

sonnet = anthropic("claude-sonnet-5", max_tokens=2048)

billing = agent(
    name="billing",
    instructions="Answer invoice questions with invoice_status. Answer asks with reply.",
    model=sonnet,
    tools=[invoice_status],
)
support = agent(
    name="support",
    instructions="Help customers. For invoice questions, ask billing.",
    model=sonnet,
)

app = host(
    store=sqlite(".threads"),
    agents={"support": support, "billing": billing},
    # One billing per tenant, in a leadless host team that never closes. A failed rebind
    # restarts it at most three times a minute; its own budget and a cancel never restart it.
    members={"billing": HostMemberOptions(restart="on_failure", max_restarts=3, within_ms=60_000)},
    message_policy=[
        # Default deny: support may only ask, and only billing. A rule to a host member may not
        # allow start, monitor or cancel — the host starts, watches and stops it.
        {
            "from": "support",
            "to": "billing",
            "allow": ["ask"],
            "budget": Budget(max_cost_nanos=usd(0.50)),
        },
    ],
    channels={
        "slack": slack(
            agent="support",
            signing_secret=secret("SLACK_SIGNING_SECRET"),
            bot_token=secret("SLACK_BOT_TOKEN"),
        ),
    },
)
