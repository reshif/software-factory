from collections import deque

import pytest

from factory.controller.mission_fsm import (AWAITING, GLOBAL_EVENTS, NO_WORKER, TERMINAL, TIMEOUTS,
                                            TRANSITIONS, InvalidTransition, Mission, all_states)


def exits(state):
    events = {e for (s, e) in TRANSITIONS if s == state}
    if state == "HELD":
        events.add("resume")
    return events


def test_every_non_terminal_state_has_an_exit():
    for state in all_states() - TERMINAL:
        assert exits(state), state


def test_terminal_states_have_no_exits():
    for state in TERMINAL:
        assert not exits(state), state
        with pytest.raises(InvalidTransition):
            m = Mission("M", state=state)
            m.fire("kill_switch")


def test_every_state_is_reachable_from_new():
    graph = {}
    for (src, _), dst in TRANSITIONS.items():
        graph.setdefault(src, set()).add(dst)
    graph.setdefault("HELD", set()).update(AWAITING | {"AWAITING_HX"})
    for target, sources in GLOBAL_EVENTS.values():
        for s in (sources or all_states() - TERMINAL - {"HELD"}):
            graph.setdefault(s, set()).add(target)
    seen, queue = {"NEW"}, deque(["NEW"])
    while queue:
        for nxt in graph.get(queue.popleft(), ()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    assert seen == set(all_states())


def test_side_states_have_timeouts():
    for state in AWAITING | {"HELD", "BLOCKED"}:
        assert state in TIMEOUTS, state
        _, event = TIMEOUTS[state]
        assert (state, event) in TRANSITIONS, state


def test_no_worker_in_waiting_states():
    for state in AWAITING | {"HELD", "BLOCKED"}:
        assert not Mission("M", state=state).worker_allowed
    assert Mission("M", state="ACTIVE").worker_allowed
    assert NO_WORKER >= TERMINAL


def test_kill_switch_then_resume_requires_hx():
    m = Mission("M", state="ACTIVE")
    m.fire("kill_switch")
    assert m.state == "HELD"
    assert m.fire("resume") == "AWAITING_HX"


def test_held_approval_resumes_to_same_gate():
    m = Mission("M", state="AWAITING_H2")
    m.fire("defer")
    assert m.fire("resume") == "AWAITING_H2"


def test_budget_exhausted_only_from_work_states():
    m = Mission("M", state="ACTIVE")
    assert m.fire("budget_exhausted") == "AWAITING_HX"
    with pytest.raises(InvalidTransition):
        Mission("M", state="AWAITING_H1").fire("budget_exhausted")


def test_unknown_event_rejected():
    with pytest.raises(InvalidTransition):
        Mission("M").fire("approved")
