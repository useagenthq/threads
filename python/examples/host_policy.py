"""Shows: three host agents, one explicit Slack route, and a message policy that lets support start
and ask billing (each started member capped at $2.00). Everything else is denied and recorded as
message_policy_decided. support has no `team` of its own: the rule that allows `start` is what makes
it a lead, and its model gets exactly the two team tools its rules allow.

Needs: ANTHROPIC_API_KEY, SLACK_SIGNING_SECRET, SLACK_BOT_TOKEN
Run: threads dev examples/host_policy.py (it exports `app`, as README.md does).
"""

from pydantic import BaseModel, Field

from threads import RunContext, agent, secret, sqlite, tool, usd
from threads.anthropic import anthropic
from threads.host import host
from threads.log import Budget
from threads.slack import slack

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
hr = agent(name="hr", instructions="Answer HR policy questions.", model=sonnet)
support = agent(
    name="support",
    instructions="Help customers. For invoice questions, start billing and ask it.",
    model=sonnet,
)

app = host(
    store=sqlite(".threads"),
    agents={"support": support, "billing": billing, "hr": hr},
    channels={
        # Three host agents: the channel must name its agent.
        "slack": slack(
            agent="support",
            signing_secret=secret("SLACK_SIGNING_SECRET"),
            bot_token=secret("SLACK_BOT_TOKEN"),
        ),
    },
    message_policy=[
        # Default deny. support -> hr, billing -> support, and any cancel are refused and recorded.
        {
            "from": "support",
            "to": "billing",
            "allow": ["start", "ask"],
            "budget": Budget(max_cost_nanos=usd(2.00)),
        },
    ],
)
