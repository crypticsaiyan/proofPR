"""OpenTelemetry tracing.

One trace per run, a span per pipeline step (started in `Ledger.append` at
`step_started`, ended at `step_finished`/`step_skipped`, so instrumentation
lives in one place instead of being threaded through every step method), and
child spans per application call and model call, per AGENTS.md section 13.

Tracing is opt-in and free when it is off: without `OTEL_EXPORTER_OTLP_ENDPOINT`
set, the global tracer provider is left at OpenTelemetry's own no-op default,
so every span created below costs nothing and goes nowhere. This mirrors how
`OPENROUTER_API_KEY` gates the real model: nothing here fabricates telemetry to
make the harness look instrumented when it is not configured to be.
"""

from __future__ import annotations

import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Tracer

_configured = False


def configure_tracing() -> None:
    """Install a real `TracerProvider` if `OTEL_EXPORTER_OTLP_ENDPOINT` is set.

    Idempotent and safe to call from every entry point (CLI commands, the
    Discord bot, the eval harness): the first call decides, every later call is
    a no-op, matching `configure_logging`'s pattern.
    """
    global _configured
    if _configured:
        return
    _configured = True

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return

    service_name = os.environ.get("OTEL_SERVICE_NAME", "proofpr")
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)


def get_tracer() -> Tracer:
    """Return this project's tracer.

    Safe to call before `configure_tracing`: OpenTelemetry's default provider
    hands back a tracer that produces no-op spans until a real one is set.
    """
    return trace.get_tracer("proofpr")


def set_genai_attributes(
    span: Span,
    *,
    system: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
) -> None:
    """Set the GenAI semantic-convention attributes AGENTS.md section 13 names.

    A model span without these is a span that cannot answer "which model, how
    many tokens, what did it cost" — the three questions a run's bill and a
    run's trace should agree on.
    """
    span.set_attribute("gen_ai.system", system)
    span.set_attribute("gen_ai.request.model", model)
    span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
    span.set_attribute("gen_ai.usage.output_tokens", output_tokens)
    span.set_attribute("proofpr.cost_usd", cost_usd)


__all__ = ["configure_tracing", "get_tracer", "set_genai_attributes"]
