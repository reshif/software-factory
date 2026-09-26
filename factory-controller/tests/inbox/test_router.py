"""FastAPI TestClient tests for the approval inbox router (final draft §10, §13.1 #6, #8)."""
import threading
from datetime import timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from factory.inbox.router import build_inbox_router

from .conftest import CONTENT_HASH, issue_token, make_approval, make_packet


def test_malicious_title_is_escaped(client, store, now, signer):
    request = make_approval(store, now)
    packet = make_packet(request, title="<script>alert('pwn')</script>")
    store.save_packet(packet)
    token = issue_token(signer, now)

    response = client.get(f"/inbox/{request.request_id}", params={"token": token})

    assert response.status_code == 200
    assert "<script>alert" not in response.text
    assert "&lt;script&gt;alert(&#x27;pwn&#x27;)&lt;/script&gt;" in response.text


def test_untrusted_diff_and_inputs_are_escaped(client, store, now, signer):
    request = make_approval(store, now)
    packet = make_packet(request)
    packet.raw_diff = "<img src=x onerror=alert(1)>"
    packet.untrusted_inputs = ("<b>issue body</b>",)
    store.save_packet(packet)
    token = issue_token(signer, now)

    response = client.get(f"/inbox/{request.request_id}", params={"token": token})

    assert response.status_code == 200
    assert "<img src=x" not in response.text
    assert "<b>issue body</b>" not in response.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in response.text


def test_tampered_token_is_rejected(client, store, now, signer):
    request = make_approval(store, now)
    store.save_packet(make_packet(request))
    token = issue_token(signer, now)
    body, signature = token.rsplit(".", 1)
    tampered = f"{body}.{signature[:-1]}{'a' if signature[-1] != 'a' else 'b'}"

    response = client.get(f"/inbox/{request.request_id}", params={"token": tampered})

    assert response.status_code == 403


def test_expired_token_is_rejected(client, store, now, signer):
    request = make_approval(store, now, expires_in=timedelta(days=1))
    store.save_packet(make_packet(request))
    # Issued an hour before `now` with a 5-minute lifetime: already expired relative to
    # the server clock (which the fixtures pin at `now`), independent of the approval's
    # own (much later) expiry.
    stale_token = issue_token(signer, now - timedelta(hours=1), expires_in=timedelta(minutes=5))

    response = client.get(f"/inbox/{request.request_id}", params={"token": stale_token})

    assert response.status_code == 410


def test_token_scoped_to_another_request_is_rejected(client, store, now, signer):
    request = make_approval(store, now)
    store.save_packet(make_packet(request))
    other_token = issue_token(signer, now, request_id="REQ-OTHER")

    response = client.get(f"/inbox/{request.request_id}", params={"token": other_token})

    assert response.status_code == 403


def test_unknown_request_id_is_404(client, signer, now):
    token = issue_token(signer, now, request_id="REQ-GHOST")

    response = client.get("/inbox/REQ-GHOST", params={"token": token})

    assert response.status_code == 404


def test_wrong_content_hash_gives_409(client, store, now, signer):
    request = make_approval(store, now)
    store.save_packet(make_packet(request))
    token = issue_token(signer, now)

    response = client.post(f"/inbox/{request.request_id}/decision",
                           data={"token": token, "decision": "approve", "content_hash": "sha256:wrong"})

    assert response.status_code == 409


def test_approving_after_expiry_gives_410(client, store, clock, now, signer):
    # The token outlives the approval request, so advancing the clock isolates the
    # store's "operation must start before expires" rule (Expired) from token expiry.
    request = make_approval(store, now, expires_in=timedelta(hours=1))
    store.save_packet(make_packet(request))
    token = issue_token(signer, now, expires_in=timedelta(hours=20))
    clock.advance(hours=2)

    response = client.post(f"/inbox/{request.request_id}/decision",
                           data={"token": token, "decision": "approve", "content_hash": CONTENT_HASH})

    assert response.status_code == 410


def test_revise_voids_the_round(client, store, now, signer):
    request = make_approval(store, now, required="2")
    store.save_packet(make_packet(request))
    tl_token = issue_token(signer, now, approver="@tl", roles=("tech_lead",))
    po_token = issue_token(signer, now, approver="@po2", roles=("product",))

    revise = client.post(f"/inbox/{request.request_id}/decision",
                         data={"token": tl_token, "decision": "revise", "content_hash": CONTENT_HASH})
    assert revise.status_code == 200

    approve = client.post(f"/inbox/{request.request_id}/decision",
                          data={"token": po_token, "decision": "approve", "content_hash": CONTENT_HASH})
    assert approve.status_code == 409


def test_decision_is_attributed_to_the_token_approver(client, store, now, signer):
    request = make_approval(store, now)
    store.save_packet(make_packet(request))
    token = issue_token(signer, now, approver="@tl", roles=("tech_lead",))

    response = client.post(f"/inbox/{request.request_id}/decision",
                           data={"token": token, "decision": "approve", "content_hash": CONTENT_HASH})

    assert response.status_code == 200
    approvers = [d.approver for d in store.approvals.get(request.request_id).approvers()]
    assert approvers == ["@tl"]


def test_ineligible_editor_gets_403(client, store, now, signer):
    request = make_approval(store, now, gate="HM", editors=("@dev",))
    store.save_packet(make_packet(request))
    token = issue_token(signer, now, approver="@dev", roles=("tech_lead",))

    response = client.post(f"/inbox/{request.request_id}/decision",
                           data={"token": token, "decision": "approve", "content_hash": CONTENT_HASH})

    assert response.status_code == 403


def test_list_inbox_shows_open_packets(client, store, now, signer):
    request = make_approval(store, now)
    store.save_packet(make_packet(request, title="Ship the widget"))
    token = issue_token(signer, now, request_id=None)

    response = client.get("/inbox", params={"token": token})

    assert response.status_code == 200
    assert "Ship the widget" in response.text


def test_general_list_token_can_view_but_cannot_decide(client, store, now, signer):
    """S5: separation of duties — a general (request_id=None) token can browse the
    inbox, but recording a decision always requires a token scoped to that request."""
    request = make_approval(store, now)
    store.save_packet(make_packet(request))
    general_token = issue_token(signer, now, request_id=None)

    view = client.get(f"/inbox/{request.request_id}", params={"token": general_token})
    assert view.status_code == 200

    decide_attempt = client.post(f"/inbox/{request.request_id}/decision",
                                 data={"token": general_token, "decision": "approve",
                                       "content_hash": CONTENT_HASH})
    assert decide_attempt.status_code == 403
    assert store.approvals.get(request.request_id).approvers() == []


def test_replayed_decision_token_is_rejected(client, store, now, signer):
    """A signed token's jti may record exactly one decision, even for an "approve"
    that the approval store itself would otherwise treat as an idempotent no-op."""
    request = make_approval(store, now, required="2")
    store.save_packet(make_packet(request))
    token = issue_token(signer, now, approver="@tl", roles=("tech_lead",))

    first = client.post(f"/inbox/{request.request_id}/decision",
                        data={"token": token, "decision": "approve", "content_hash": CONTENT_HASH})
    assert first.status_code == 200

    replay = client.post(f"/inbox/{request.request_id}/decision",
                         data={"token": token, "decision": "approve", "content_hash": CONTENT_HASH})
    assert replay.status_code == 409

    approvers = [d.approver for d in store.approvals.get(request.request_id).approvers()]
    assert approvers == ["@tl"]  # the replay recorded nothing new


def test_links_respect_the_mount_prefix(store, decide, signer, clock, now):
    """Low: rendered links use `request.url_for`, so they still work when the router
    is mounted under a prefix instead of at the app root."""
    request = make_approval(store, now)
    store.save_packet(make_packet(request, title="Ship the widget"))

    application = FastAPI()
    application.include_router(build_inbox_router(store, decide, signer, clock), prefix="/mounted")
    mounted_client = TestClient(application)

    list_token = issue_token(signer, now, request_id=None)
    list_response = mounted_client.get("/mounted/inbox", params={"token": list_token})
    assert list_response.status_code == 200
    assert f"/mounted/inbox/{request.request_id}?token=" in list_response.text

    packet_token = issue_token(signer, now)
    packet_response = mounted_client.get(f"/mounted/inbox/{request.request_id}",
                                         params={"token": packet_token})
    assert packet_response.status_code == 200
    assert f'action="http://testserver/mounted/inbox/{request.request_id}/decision"' in packet_response.text

    decision = mounted_client.post(f"/mounted/inbox/{request.request_id}/decision",
                                   data={"token": packet_token, "decision": "approve",
                                         "content_hash": CONTENT_HASH})
    assert decision.status_code == 200


def test_approver_missing_from_eligible_list_gets_403(client, store, now, signer):
    """Red team #3: `ApprovalRequest.eligible` (product config), not a role a token
    merely claims, decides who may approve — and a clear 403, not a generic error."""
    request = make_approval(store, now, eligible={"@tl": ("tech_lead",), "@sec": ("security",)})
    store.save_packet(make_packet(request))
    token = issue_token(signer, now, approver="@rando", roles=("tech_lead",))

    response = client.post(f"/inbox/{request.request_id}/decision",
                           data={"token": token, "decision": "approve", "content_hash": CONTENT_HASH})

    assert response.status_code == 403
    assert "not a listed approver" in response.json()["detail"]
    assert store.approvals.get(request.request_id).approvers() == []


def test_eligible_approver_is_unaffected_by_claimed_roles(client, store, now, signer):
    """A token claiming a role it doesn't have (e.g. "security") is ignored: the
    config's roles for that approver are what's actually recorded and checked."""
    request = make_approval(store, now, required="1+sec",
                            eligible={"@tl": ("security",)})  # config grants @tl the security role
    store.save_packet(make_packet(request))
    # The token claims no roles at all; the config still grants @tl "security".
    token = issue_token(signer, now, approver="@tl", roles=())

    response = client.post(f"/inbox/{request.request_id}/decision",
                           data={"token": token, "decision": "approve", "content_hash": CONTENT_HASH})

    assert response.status_code == 200
    decision = store.approvals.get(request.request_id).approvers()[0]
    assert decision.roles == frozenset({"security"})


def test_list_hides_requests_the_approver_is_not_eligible_for(client, store, now, signer):
    open_to_tl = make_approval(store, now, request_id="REQ-OPEN", eligible={"@tl": ("tech_lead",)})
    store.save_packet(make_packet(open_to_tl, title="Only @tl can see this"))
    unrestricted = make_approval(store, now, request_id="REQ-UNRESTRICTED", eligible=None)
    store.save_packet(make_packet(unrestricted, title="Anyone can see this"))

    outsider_token = issue_token(signer, now, approver="@rando", roles=(), request_id=None)
    response = client.get("/inbox", params={"token": outsider_token})

    assert response.status_code == 200
    assert "Only @tl can see this" not in response.text
    assert "Anyone can see this" in response.text


def test_list_shows_requests_the_approver_is_eligible_for(client, store, now, signer):
    request = make_approval(store, now, eligible={"@tl": ("tech_lead",)})
    store.save_packet(make_packet(request, title="Ship the widget"))

    tl_token = issue_token(signer, now, approver="@tl", roles=(), request_id=None)
    response = client.get("/inbox", params={"token": tl_token})

    assert response.status_code == 200
    assert "Ship the widget" in response.text


def test_concurrent_replayed_decision_records_only_one(client, store, now, signer):
    """L1: `use_once` must be an atomic check-and-set, not a racy check-then-act.

    Fire the same token at the decision endpoint from two threads at once. Exactly
    one may get through; the other must see the replay rejection (409), and the
    store must end up with exactly one recorded decision either way.
    """
    request = make_approval(store, now, required="2")
    store.save_packet(make_packet(request))
    token = issue_token(signer, now, approver="@tl", roles=("tech_lead",))

    statuses: list[int] = []
    barrier = threading.Barrier(2)

    def _post() -> None:
        barrier.wait()
        response = client.post(f"/inbox/{request.request_id}/decision",
                               data={"token": token, "decision": "defer", "content_hash": CONTENT_HASH})
        statuses.append(response.status_code)

    threads = [threading.Thread(target=_post) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(statuses) == [200, 409]
    assert len(store.approvals.get(request.request_id).decisions) == 1
