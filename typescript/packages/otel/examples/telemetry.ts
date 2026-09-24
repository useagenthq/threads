import { agent, scriptedModel, sqlite } from "@threads/core";
import { otel } from "@threads/otel";

// OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 bun examples/telemetry.ts
const store = sqlite(".threads");
const bot = agent({
  model: scriptedModel({
    responses: [
      {
        content: [{ type: "text", text: "Hello!" }],
        stop_reason: "end_turn",
        usage: { input_tokens: 12, output_tokens: 3 },
      },
    ],
  }),
});
await bot.run("Hi.", { store });

const exporter = otel({ store, service: "support-bot" });
const sent = await exporter.sync();
if (!sent.ok) console.error(sent.error.code, sent.error.message);
else console.log(sent.value.spans, "spans exported");
