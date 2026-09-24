import type { Exporter, Store } from "@threads/core";
import { resolve } from "./env";
import { OtelExporter } from "./exporter";

// otel() (spec/api.json): traces derived from the log, sent to an OpenTelemetry collector over
// OTLP/HTTP JSON. No OpenTelemetry SDK: span ids come from event ids, so a re-send carries the
// same ids, and the bytes match the Python exporter's.

export type OtelOptions = {
  /** The store to export, every tenant. Omitted: the store of the host it is passed to. */
  readonly store?: Store;
  /** The collector's traces URL. Omitted: OTEL_EXPORTER_OTLP_TRACES_ENDPOINT or OTEL_EXPORTER_OTLP_ENDPOINT. */
  readonly endpoint?: string;
  /** Request headers (credentials: never logged). Omitted: the OTEL_EXPORTER_OTLP_*HEADERS variables. */
  readonly headers?: Record<string, string>;
  /** service.name. Omitted: OTEL_SERVICE_NAME, else OTEL_RESOURCE_ATTRIBUTES, else threads. */
  readonly service?: string;
  /** Names this exporter's cursor (observer otel:<name>). Omitted: otel. */
  readonly name?: string;
  /** true: tool arguments and results and response text, as logged. Default false. */
  readonly content?: boolean;
};

/** An Exporter for host({telemetry: otel()}), or to call sync() on yourself. */
export function otel(options: OtelOptions = {}): Exporter {
  const config = resolve(options, process.env, "nodejs");
  const observer = options.name === undefined ? "otel" : `otel:${options.name}`;
  return new OtelExporter(
    config,
    options.store,
    observer,
    options.content ?? false,
  );
}
