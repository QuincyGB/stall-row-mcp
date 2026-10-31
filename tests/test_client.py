"""Client payment-flow tests. ALL payments are mocked — the HTTP layer is
an httpx MockTransport and the signer is a throwaway account generated
at test time. No key material lives in this repo, and no test touches
the network or spends anything.

The 402 fixtures are real: captured from unpaid probes of production on
2026-10-27 (wallet-verdict and pre-trade-guard). The response fixtures
are the service's own published output examples.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from stall_row_mcp.client import (
    ROUTE_PRICES,
    TRANSFER_WITH_AUTHORIZATION_TYPES,
    PaymentRequiredError,
    StallRowClient,
    StallRowError,
    build_authorization,
    load_account_from_env,
    parse_payment_required,
    select_requirement,
)
from stall_row_mcp.server import run_pre_trade_guard, run_wallet_verdict

FIXTURES = Path(__file__).parent / "fixtures"
CHALLENGE = json.loads((FIXTURES / "payment_required.json").read_text())
GUARD_CHALLENGE = json.loads(
    (FIXTURES / "pre_trade_guard_payment_required.json").read_text()
)
VERDICT = json.loads((FIXTURES / "wallet_verdict_example.json").read_text())
GUARD = json.loads((FIXTURES / "pre_trade_guard_example.json").read_text())
USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PAY_TO = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
ADDR = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"


def challenge_response(challenge: dict) -> httpx.Response:
    header = base64.b64encode(json.dumps(challenge).encode()).decode()
    return httpx.Response(
        402, headers={"payment-required": header}, json={}, request=_req()
    )


def _req() -> httpx.Request:
    return httpx.Request("POST", "https://example.test/v1/wallet-verdict")


class Recorder:
    """MockTransport handler: replays queued responses, records requests."""

    def __init__(self, responses: list[httpx.Response]):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)


def mock_client(recorder: Recorder, **kwargs) -> StallRowClient:
    http = httpx.Client(transport=httpx.MockTransport(recorder))
    return StallRowClient(base_url="https://example.test", http=http, **kwargs)


def _receipt(account) -> str:
    return base64.b64encode(
        json.dumps(
            {
                "success": True,
                "transaction": "0x" + "ab" * 32,
                "network": "eip155:8453",
                "payer": account.address,
            }
        ).encode()
    ).decode()


# --- challenge parsing -------------------------------------------------------


def test_parse_real_production_challenge():
    parsed = parse_payment_required(challenge_response(CHALLENGE))
    assert parsed["x402Version"] == 2
    req = select_requirement(parsed)
    assert req["scheme"] == "exact"
    assert req["network"] == "eip155:8453"
    assert req["asset"] == USDC_ADDRESS
    assert req["amount"] == "20000"  # wallet-verdict: $0.02
    assert req["payTo"] == PAY_TO


def test_route_prices_cover_all_paid_routes():
    # Mirrors the service's own PRICE_UNITS (app/config.py in stall-row).
    assert ROUTE_PRICES["/v1/pre-trade-guard"] == 5_000
    assert ROUTE_PRICES["/v1/orchestrate"] == 750_000
    assert ROUTE_PRICES["/v1/fleet-pass"] == 5_000_000
    assert ROUTE_PRICES["/v1/sign-read"] == 10_000
    assert ROUTE_PRICES["/v1/score-book"] == 10_000
    assert ROUTE_PRICES["/v1/guard"] == 10_000
    assert len(ROUTE_PRICES) == 20


# --- no signer: structured error, never a payment ----------------------------


def test_no_signer_returns_structured_error_and_never_retries():
    recorder = Recorder([challenge_response(CHALLENGE)])
    client = mock_client(recorder, account=None)
    result = run_wallet_verdict(ADDR, client=client)
    assert result["error"] == "payment_required"
    assert result["accepts"][0]["amount"] == "20000"
    assert len(recorder.requests) == 1


# --- signer flow ---------------------------------------------------------------


def test_paid_flow_signs_eip3009_and_retries():
    account = Account.create()  # throwaway, generated at test time
    ok = httpx.Response(
        200,
        headers={"payment-response": _receipt(account)},
        json=VERDICT,
        request=_req(),
    )
    recorder = Recorder([challenge_response(CHALLENGE), ok])
    client = mock_client(recorder, account=account)

    result = client.wallet_verdict(ADDR)
    assert result["verdict"] in ("SAFE", "CAUTION", "AVOID")
    assert client.last_receipt["transaction"] == "0x" + "ab" * 32
    assert len(recorder.requests) == 2

    retry = recorder.requests[1]
    header = retry.headers.get("PAYMENT-SIGNATURE")
    assert header, "paid retry must carry the PAYMENT-SIGNATURE header"
    payload = json.loads(base64.b64decode(header))
    assert payload["accepted"]["amount"] == "20000"
    auth = payload["payload"]["authorization"]
    assert auth["from"] == account.address
    assert auth["to"] == PAY_TO
    assert auth["value"] == "20000"

    req = select_requirement(CHALLENGE)
    domain = {
        "name": req["extra"]["name"],
        "version": req["extra"]["version"],
        "chainId": 8453,
        "verifyingContract": req["asset"],
    }
    message = {
        "from": auth["from"],
        "to": auth["to"],
        "value": int(auth["value"]),
        "validAfter": int(auth["validAfter"]),
        "validBefore": int(auth["validBefore"]),
        "nonce": bytes.fromhex(auth["nonce"].removeprefix("0x")),
    }
    encoded = encode_typed_data(domain, TRANSFER_WITH_AUTHORIZATION_TYPES, message)
    recovered = Account.recover_message(encoded, signature=payload["payload"]["signature"])
    assert recovered == account.address


def test_pre_trade_guard_paid_flow_uses_its_own_price():
    account = Account.create()
    ok = httpx.Response(
        200,
        headers={"payment-response": _receipt(account)},
        json=GUARD,
        request=_req(),
    )
    recorder = Recorder([challenge_response(GUARD_CHALLENGE), ok])
    client = mock_client(recorder, account=account)

    result = run_pre_trade_guard(PAY_TO, amount=20000, client=client)
    assert result["verdict"] in ("PROCEED", "CAUTION", "ABORT")
    # The first (unpaid) request carried the guard's input shape.
    body = json.loads(recorder.requests[0].content)
    assert body == {"pay_to": PAY_TO, "amount": 20000}
    payload = json.loads(
        base64.b64decode(recorder.requests[1].headers["PAYMENT-SIGNATURE"])
    )
    assert payload["accepted"]["amount"] == "5000"  # $0.005 guard price


def test_spend_guard_refuses_above_route_price():
    expensive = json.loads(json.dumps(CHALLENGE))
    expensive["accepts"][0]["amount"] = "25000"  # $0.025 > $0.02 route guard
    recorder = Recorder([challenge_response(expensive)])
    client = mock_client(recorder, account=Account.create())
    with pytest.raises(PaymentRequiredError) as excinfo:
        client.wallet_verdict(ADDR)
    assert "spend guard" in excinfo.value.reason
    assert len(recorder.requests) == 1


def test_global_ceiling_override_allows_operator_choice():
    expensive = json.loads(json.dumps(CHALLENGE))
    expensive["accepts"][0]["amount"] = "25000"
    account = Account.create()
    ok = httpx.Response(200, json=VERDICT, request=_req())
    recorder = Recorder([challenge_response(expensive), ok])
    client = mock_client(recorder, account=account, max_amount_units=30_000)
    result = client.wallet_verdict(ADDR)
    assert result["verdict"] in ("SAFE", "CAUTION", "AVOID")


def test_paid_retry_still_402_raises():
    recorder = Recorder(
        [challenge_response(CHALLENGE), challenge_response(CHALLENGE)]
    )
    client = mock_client(recorder, account=Account.create())
    with pytest.raises(PaymentRequiredError) as excinfo:
        client.wallet_verdict(ADDR)
    assert "still returned 402" in excinfo.value.reason


# --- served-without-payment + free routes -------------------------------------


def test_first_200_needs_no_payment():
    # e.g. a recheck covered by a Recheck Pack balance: 200 on the
    # first POST, returned as-is, no signer involved.
    body = {"ok": True, "url": "https://x.example/v1/y", "payable": True}
    ok = httpx.Response(200, json=body, request=_req())
    recorder = Recorder([ok])
    client = mock_client(recorder, account=None)
    assert client.recheck("https://x.example/v1/y", pack_id="rp_test") == body
    assert len(recorder.requests) == 1


def test_free_balance_read_posts_without_payment():
    body = {"ok": True, "pack_id": "rp_test", "checks_remaining": 249}
    ok = httpx.Response(200, json=body, request=_req())
    recorder = Recorder([ok])
    client = mock_client(recorder, account=None)
    assert client.recheck_pack_balance("rp_test") == body
    assert json.loads(recorder.requests[0].content) == {"pack_id": "rp_test"}


# --- non-payment failures -----------------------------------------------------


def test_upstream_error_maps_to_stallrow_error():
    not_found = httpx.Response(404, json={"error": "not found"}, request=_req())
    client = mock_client(Recorder([not_found]), account=None)
    with pytest.raises(StallRowError) as excinfo:
        client.gas_oracle()
    assert excinfo.value.status == 404


# --- signer loading ------------------------------------------------------------


def test_load_account_from_env_variants(tmp_path):
    assert load_account_from_env({}) is None
    account = Account.create()
    key = account.key.hex()
    loaded = load_account_from_env({"AGENT_PRIVATE_KEY": key})
    assert loaded is not None and loaded.address == account.address
    key_file = tmp_path / "agent.key"
    key_file.write_text(key + "\n")
    loaded = load_account_from_env({"AGENT_KEY_FILE": str(key_file)})
    assert loaded is not None and loaded.address == account.address


def test_build_authorization_window():
    req = select_requirement(CHALLENGE)
    auth = build_authorization("0x" + "11" * 20, req, now=1_000_000)
    assert auth["validAfter"] == "0"
    assert auth["validBefore"] == str(1_000_000 + 300)
    assert auth["value"] == "20000"
    assert auth["to"] == PAY_TO
