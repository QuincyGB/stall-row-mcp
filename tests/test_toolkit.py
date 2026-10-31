"""Inspector-toolkit tools: sign-read / score-book / guard ($0.01 each)
plus the free Recheck feed v2 read. All payments are mocked — httpx
MockTransport, throwaway signer — exactly like test_client.py. The
response fixtures are the service's own published output examples
(app/discovery.py ROUTES in QuincyGB/stall-row); the 402 fixture is
the real production challenge shape with the amount set to the
toolkit price (10000 units = $0.01).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
from eth_account import Account

from stall_row_mcp.client import PaymentRequiredError, StallRowClient
from stall_row_mcp.schemas import ROUTE_SCHEMAS
from stall_row_mcp.server import (
    run_guard,
    run_recheck_feed,
    run_score_book,
    run_sign_read,
)

FIXTURES = Path(__file__).parent / "fixtures"
CHALLENGE = json.loads((FIXTURES / "toolkit_payment_required.json").read_text())
SIGN_READ = json.loads((FIXTURES / "sign_read_example.json").read_text())
SCORE_BOOK = json.loads((FIXTURES / "score_book_example.json").read_text())
GUARD = json.loads((FIXTURES / "guard_example.json").read_text())
TARGET = "https://example.com/v1/verdict"


def _req() -> httpx.Request:
    return httpx.Request("POST", "https://example.test/v1/sign-read")


def challenge_response(challenge: dict) -> httpx.Response:
    header = base64.b64encode(json.dumps(challenge).encode()).decode()
    return httpx.Response(
        402, headers={"payment-required": header}, json={}, request=_req()
    )


class Recorder:
    def __init__(self, responses: list[httpx.Response]):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)


def mock_client(recorder: Recorder, **kwargs) -> StallRowClient:
    http = httpx.Client(transport=httpx.MockTransport(recorder))
    return StallRowClient(base_url="https://example.test", http=http, **kwargs)


# --- schemas: the examples satisfy the published output contracts -----------


def test_toolkit_examples_satisfy_output_schemas():
    for path, example in (
        ("/v1/sign-read", SIGN_READ),
        ("/v1/score-book", SCORE_BOOK),
        ("/v1/guard", GUARD),
    ):
        for key in ROUTE_SCHEMAS[path]["output"]["required"]:
            assert key in example, (path, key)


# --- no signer: structured error, never a payment ----------------------------


def test_guard_no_signer_returns_structured_error():
    recorder = Recorder([challenge_response(CHALLENGE)])
    client = mock_client(recorder, account=None)
    result = run_guard(TARGET, client=client)
    assert result["error"] == "payment_required"
    assert result["accepts"][0]["amount"] == "10000"
    assert len(recorder.requests) == 1


# --- paid flows at exactly $0.01 ----------------------------------------------


def test_sign_read_paid_flow_uses_toolkit_price_and_body():
    account = Account.create()
    ok = httpx.Response(200, json=SIGN_READ, request=_req())
    recorder = Recorder([challenge_response(CHALLENGE), ok])
    client = mock_client(recorder, account=account)

    result = run_sign_read(
        TARGET, declared_amount="5000", client=client
    )
    assert result["challenge_found"] is True
    body = json.loads(recorder.requests[0].content)
    assert body == {"url": TARGET, "declared_amount": "5000"}
    payload = json.loads(
        base64.b64decode(recorder.requests[1].headers["PAYMENT-SIGNATURE"])
    )
    assert payload["accepted"]["amount"] == "10000"  # $0.01 toolkit price
    assert payload["payload"]["authorization"]["value"] == "10000"


def test_score_book_paid_flow_sends_exactly_one_target():
    account = Account.create()
    ok = httpx.Response(200, json=SCORE_BOOK, request=_req())
    recorder = Recorder([challenge_response(CHALLENGE), ok])
    client = mock_client(recorder, account=account)

    result = run_score_book(url=TARGET, client=client)
    assert result["found"] is True
    assert json.loads(recorder.requests[0].content) == {"url": TARGET}


def test_toolkit_spend_guard_refuses_above_10000():
    expensive = json.loads(json.dumps(CHALLENGE))
    expensive["accepts"][0]["amount"] = "10001"
    recorder = Recorder([challenge_response(expensive)])
    client = mock_client(recorder, account=Account.create())
    with pytest.raises(PaymentRequiredError) as excinfo:
        client.guard(TARGET)
    assert "spend guard" in excinfo.value.reason
    assert len(recorder.requests) == 1


# --- the free feed read ---------------------------------------------------------


def test_recheck_feed_parses_envelopes_and_chain_head():
    envelopes = [
        {"seq": 41, "record_id": "r41", "schema_version": 2,
         "prev_hash": "a", "record_hash": "b", "record": {"url": TARGET}},
        {"seq": 42, "record_id": "r42", "schema_version": 2,
         "prev_hash": "b", "record_hash": "c", "record": {"url": TARGET}},
    ]
    body = "".join(json.dumps(e) + "\n" for e in envelopes)
    resp = httpx.Response(
        200,
        headers={
            "X-Recheck-Feed-Latest-Seq": "42",
            "X-Recheck-Feed-Count": "42",
            "content-type": "application/x-ndjson",
        },
        text=body,
        request=httpx.Request("GET", "https://example.test/v1/recheck/feed"),
    )
    recorder = Recorder([resp])
    client = mock_client(recorder, account=None)

    result = run_recheck_feed(since_seq=40, client=client)
    assert result["format"] == "recheck-feed-v2"
    assert result["count"] == 2
    assert result["latest_seq"] == 42
    assert result["records"][1]["prev_hash"] == result["records"][0]["record_hash"]
    sent = recorder.requests[0]
    assert sent.method == "GET"
    assert sent.url.params["v"] == "2"
    assert sent.url.params["since_seq"] == "40"


def test_recheck_feed_empty_history_is_empty_result():
    resp = httpx.Response(
        200,
        headers={"X-Recheck-Feed-Latest-Seq": "0", "X-Recheck-Feed-Count": "0"},
        text="",
        request=httpx.Request("GET", "https://example.test/v1/recheck/feed"),
    )
    client = mock_client(Recorder([resp]), account=None)
    result = run_recheck_feed(client=client)
    assert result["count"] == 0 and result["records"] == []
