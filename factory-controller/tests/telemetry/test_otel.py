"""telemetry.otel: no-op when opentelemetry is absent.

`opentelemetry-api` happens to be present transitively in this dev environment (pulled
in by some other dependency), so `otel.OTEL_AVAILABLE` may be True here even though
`factory-controller` declares no dependency on it. The "absent" branch is therefore
exercised explicitly, via `monkeypatch.setattr(otel, "OTEL_AVAILABLE", False)`, rather
than by relying on the package being uninstalled -- and every assertion below checks a
concrete return value or type, not just "no exception was raised".
"""
from factory.telemetry import otel


def test_no_op_span_yields_a_no_op_span_with_no_dependency_on_otel(monkeypatch):
    monkeypatch.setattr(otel, "OTEL_AVAILABLE", False)
    monkeypatch.setattr(otel, "_TRACER_PROVIDER", None)

    with otel.span("verify.review", mission_id="MIS-1") as span:
        assert isinstance(span, otel._NoOpSpan)


def test_no_op_get_tracer_returns_a_no_op_tracer(monkeypatch):
    monkeypatch.setattr(otel, "OTEL_AVAILABLE", False)
    monkeypatch.setattr(otel, "_TRACER_PROVIDER", None)

    tracer = otel.get_tracer("factory")
    assert isinstance(tracer, otel._NoOpTracer)
    with tracer.start_as_current_span("op") as span:
        assert isinstance(span, otel._NoOpSpan)


def test_no_op_span_methods_are_genuinely_inert():
    span = otel._NoOpSpan()
    assert span.set_attribute("k", "v") is None
    assert span.record_exception(RuntimeError("boom")) is None
    assert span.set_status("ok", "extra", kw=1) is None


def test_span_propagates_exceptions_from_the_body(monkeypatch):
    monkeypatch.setattr(otel, "OTEL_AVAILABLE", False)
    monkeypatch.setattr(otel, "_TRACER_PROVIDER", None)

    class Boom(Exception):
        pass

    raised = False
    try:
        with otel.span("x"):
            raise Boom("boom")
    except Boom:
        raised = True
    assert raised, "expected Boom to propagate out of the no-op span's context manager"


def test_span_applies_every_keyword_as_an_attribute_via_the_real_otel_branch(monkeypatch):
    """`span()` must apply every keyword through `set_attribute` on the span it gets
    back from the tracer provider -- pinned here by substituting a recording tracer
    provider for `_TRACER_PROVIDER`, exercising the same "OTEL_AVAILABLE" branch a
    real opentelemetry install would take."""
    calls: list[tuple[str, object]] = []

    class RecordingSpan:
        def set_attribute(self, key, value):
            calls.append((key, value))

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    class RecordingTracer:
        def start_as_current_span(self, name, **kwargs):
            return RecordingSpan()

    class RecordingProvider:
        def get_tracer(self, name):
            return RecordingTracer()

    monkeypatch.setattr(otel, "OTEL_AVAILABLE", True)
    monkeypatch.setattr(otel, "_TRACER_PROVIDER", RecordingProvider())

    with otel.span("verify.review", mission_id="MIS-1", attempt=2):
        pass

    assert ("mission_id", "MIS-1") in calls
    assert ("attempt", 2) in calls


def test_get_tracer_real_path_delegates_to_the_tracer_provider(monkeypatch):
    sentinel_tracer = object()

    class FakeProvider:
        def get_tracer(self, name):
            assert name == "factory"
            return sentinel_tracer

    monkeypatch.setattr(otel, "OTEL_AVAILABLE", True)
    monkeypatch.setattr(otel, "_TRACER_PROVIDER", FakeProvider())

    assert otel.get_tracer("factory") is sentinel_tracer
