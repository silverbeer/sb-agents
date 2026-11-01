"""One call per process: `setup_telemetry("docs-agent")`.

Environment:
    OTEL_EXPORTER_OTLP_ENDPOINT  export traces/metrics/logs over OTLP/HTTP (e.g.
                                 http://localhost:4318 for `make obs-up`). Unset =
                                 no export, which keeps tests and CI offline.
    SB_OBS_CAPTURE_CONTENT       1 = prompts/answers in telemetry. Default off:
                                 Slack messages can hold anything.
    SB_ENV                       deployment.environment.name (default "local").
    SB_OBS_LOG_FORMAT            json (default, Cloud Logging shape) | text.
    SB_OBS_GCP_PROJECT           full Cloud Logging trace resource names.
    LOG_LEVEL                    root log level (default INFO).

Sinks are pluggable through OTel's own interfaces: pass extra `span_processors`,
`metric_readers` or `log_handlers` (a work data-bus adapter is one of those).
Agent code never imports an exporter.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version

from opentelemetry import _logs, metrics, trace
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from sb_obs.logs import GcpJsonFormatter

log = logging.getLogger("sb_obs")

_TRUTHY = {"1", "true", "yes", "on"}
# Loggers that are chatty at INFO and add nothing the spans don't already show.
# httpx2: the HTTP client the anthropic SDK ships with.
_QUIET = ("httpx", "httpx2", "httpcore", "opentelemetry.exporter")


@dataclass(frozen=True)
class Telemetry:
    service_name: str
    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    logger_provider: LoggerProvider | None
    capture_content: bool
    exporting: bool

    def shutdown(self) -> None:
        """Flush and stop exporters (they also flush at interpreter exit)."""
        self.tracer_provider.shutdown()
        self.meter_provider.shutdown()
        if self.logger_provider:
            self.logger_provider.shutdown()


_current: Telemetry | None = None


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def setup_telemetry(
    service_name: str,
    *,
    span_processors: Sequence[SpanProcessor] = (),
    metric_readers: Sequence[MetricReader] = (),
    log_handlers: Sequence[logging.Handler] = (),
    capture_content: bool | None = None,
) -> Telemetry:
    """Install global OTel providers, JSON logging and framework instrumentation.

    Idempotent: OTel global providers can only be set once per process, so a
    second call returns the first result.
    """
    global _current
    if _current is not None:
        if _current.service_name != service_name:
            log.warning("telemetry already set up as %r", _current.service_name)
        return _current

    exporting = bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))
    if capture_content is None:
        capture_content = env_flag("SB_OBS_CAPTURE_CONTENT")
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.namespace": "sb-agents",
            "service.version": _version(service_name),
            "deployment.environment.name": os.environ.get("SB_ENV", "local"),
        }
    )

    tracer_provider = TracerProvider(resource=resource)
    for processor in span_processors:
        tracer_provider.add_span_processor(processor)
    readers = list(metric_readers)
    logger_provider = None
    if exporting:
        # Imported here so the exporter stack only loads when it is used.
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        readers.append(PeriodicExportingMetricReader(OTLPMetricExporter()))
        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
        _logs.set_logger_provider(logger_provider)
    meter_provider = MeterProvider(resource=resource, metric_readers=readers)
    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(meter_provider)

    _setup_logging(service_name, logger_provider, log_handlers)
    _set_content_capture_env(capture_content)
    _instrument_pydantic_ai(tracer_provider, meter_provider, capture_content)
    _instrument_httpx(tracer_provider, meter_provider)

    _current = Telemetry(
        service_name, tracer_provider, meter_provider, logger_provider, capture_content, exporting
    )
    log.info(
        "telemetry ready",
        extra={"exporting": exporting, "capture_content": capture_content},
    )
    return _current


def instrument_asgi(app):
    """Server spans for an ASGI app (fasta2a's FastA2A); continues an incoming traceparent.

    Uses the ASGI middleware directly rather than StarletteInstrumentor: only the
    middleware can drop the per-chunk "http send/receive" child spans.
    """
    from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware

    app.add_middleware(
        OpenTelemetryMiddleware,
        exclude_spans=["receive", "send"],
        tracer_provider=trace.get_tracer_provider(),
        meter_provider=metrics.get_meter_provider(),
    )
    return app


def _version(service_name: str) -> str:
    try:
        return version(service_name)
    except PackageNotFoundError:
        return "0.0.0"


def _setup_logging(
    service_name: str,
    logger_provider: LoggerProvider | None,
    extra_handlers: Sequence[logging.Handler],
) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

    # stderr, not stdout: CLIs print their answer on stdout.
    console = logging.StreamHandler(sys.stderr)
    if os.environ.get("SB_OBS_LOG_FORMAT", "json") == "text":
        console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    else:
        console.setFormatter(GcpJsonFormatter(service_name))
    root.addHandler(console)

    if logger_provider is not None:
        from opentelemetry.instrumentation.logging.handler import LoggingHandler

        root.addHandler(LoggingHandler(logger_provider=logger_provider))
    for handler in extra_handlers:
        root.addHandler(handler)
    for name in _QUIET:
        logging.getLogger(name).setLevel(logging.WARNING)


def _set_content_capture_env(capture: bool) -> None:
    """Make ADK follow our flag. ADK reads these env vars when it emits spans.

    ADK puts prompt/response content in its spans by default
    (ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS defaults to true), so leaving these
    unset would leak content even with our flag off.
    """
    value = "true" if capture else "false"
    os.environ["ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"] = value
    os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = value


def _instrument_pydantic_ai(
    tracer_provider: TracerProvider, meter_provider: MeterProvider, capture: bool
) -> None:
    try:
        from pydantic_ai import Agent
        from pydantic_ai.models.instrumented import InstrumentationSettings
    except ImportError:  # e.g. an ADK-only process
        return
    Agent.instrument_all(
        InstrumentationSettings(
            tracer_provider=tracer_provider,
            meter_provider=meter_provider,
            include_content=capture,
            include_binary_content=capture,
        )
    )


def _instrument_httpx(tracer_provider: TracerProvider, meter_provider: MeterProvider) -> None:
    # Client spans for every httpx call, and W3C traceparent on outgoing requests:
    # that header is what carries a trace across the A2A hop.
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    HTTPXClientInstrumentor().instrument(
        tracer_provider=tracer_provider, meter_provider=meter_provider
    )
