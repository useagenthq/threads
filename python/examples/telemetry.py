import asyncio

from pydantic import JsonValue

from threads import agent, scripted_model, sqlite
from threads.otel import otel
from threads.result import Err, Ok


# OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 python examples/telemetry.py
async def main() -> None:
    store = sqlite(".threads")
    reply: JsonValue = {
        "content": [{"type": "text", "text": "Hello!"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 12, "output_tokens": 3},
    }
    bot = agent(model=scripted_model({"responses": [reply]}))
    await bot.run("Hi.", store=store)

    exporter = otel(store=store, service="support-bot")
    match await exporter.sync():
        case Ok(value):
            print(value.spans, "spans exported")
        case Err(error):
            print(error.code, error.message)


asyncio.run(main())
