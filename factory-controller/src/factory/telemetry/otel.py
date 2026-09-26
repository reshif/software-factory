"""Optional OpenTelemetry tracing helpers that no-op when `opentelemetry` isn't installed.

Nothing in `factory-controller` depends on OpenTelemetry being present. When it is
installed (an operator's deployment choice), `span()` creates real spans; otherwise it
silently does nothing, so the rest of the codebase can call it unconditionally.
"""
import logging
from contextlib import contextmanager
from typing import Iterator

logger = logging.getLogger(__name__)

try:
    from opentelemetry import trace as _otel_trace
    _TRACER_PROVIDER = _otel_trace
    OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover -- exercised whenever opentelemetry isn't installed
    _TRACER_PROVIDER = None
    OTEL_AVAILABLE = False


class _NoOpSpan:
    def set_attribute(self, key: str, value) -> None:
        pass

    def record_exception(self, exc: BaseException) -> None:
        pass

    def set_status(self, *args, **kwargs) -> None:
        pass


@contextmanager
def span(name: str, **attributes) -> Iterator[object]:
    """A context manager yielding a span-like object. No-op without OpenTelemetry.

    Usage: ``with span("verify.review", mission_id=mission_id): ...``
    """
    if not OTEL_AVAILABLE:
        yield _NoOpSpan()
        return

    tracer = _TRACER_PROVIDER.get_tracer("factory")
    with tracer.start_as_current_span(name) as otel_span:
        for key, value in attributes.items():
            otel_span.set_attribute(key, value)
        yield otel_span


def get_tracer(name: str = "factory"):
    """Return a real tracer if OpenTelemetry is installed, else an object whose
    `start_as_current_span` yields a no-op span (usable the same way as `span()`)."""
    if OTEL_AVAILABLE:
        return _TRACER_PROVIDER.get_tracer(name)
    return _NoOpTracer()


class _NoOpTracer:
    @contextmanager
    def start_as_current_span(self, name: str, **kwargs) -> Iterator[object]:
        yield _NoOpSpan()
