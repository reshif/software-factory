from factory.telemetry import otel


def test_span_is_a_context_manager_regardless_of_otel_availability():
    with otel.span("verify.review", mission_id="MIS-1") as span:
        span.set_attribute("k", "v")


def test_span_propagates_exceptions_from_the_body():
    class Boom(Exception):
        pass

    try:
        with otel.span("x"):
            raise Boom("boom")
    except Boom:
        pass
    else:
        raise AssertionError("expected Boom to propagate")


def test_get_tracer_is_usable_regardless_of_otel_availability():
    tracer = otel.get_tracer("factory")
    with tracer.start_as_current_span("op") as span:
        span.set_attribute("a", 1)


def test_no_op_path_when_otel_is_forced_absent(monkeypatch):
    """Simulate `opentelemetry` not being installed, regardless of this dev
    environment's actual dependency closure, and verify the fallback no-op path."""
    monkeypatch.setattr(otel, "OTEL_AVAILABLE", False)
    monkeypatch.setattr(otel, "_TRACER_PROVIDER", None)

    with otel.span("verify.review", mission_id="MIS-1") as span:
        span.set_attribute("k", "v")
        span.record_exception(RuntimeError("x"))
        span.set_status("ok")
    assert isinstance(otel.get_tracer(), otel._NoOpTracer)


def test_no_op_span_swallows_arbitrary_calls():
    no_op = otel._NoOpSpan()
    no_op.set_attribute("a", 1)
    no_op.record_exception(RuntimeError("boom"))
    no_op.set_status("ok", "extra", kw=1)
