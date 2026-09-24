"""An `--agent` module whose support agent changed its instructions since the cases were saved."""

from threads import agent, scripted_model

agent = agent(
    name="support",
    instructions="You answer order and billing questions.",
    model=scripted_model({"responses": []}),
)
