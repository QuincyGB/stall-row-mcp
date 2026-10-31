"""StallRow x402 client — the 402 -> sign -> retry flow.

This mirrors, field for field, the proven client flow used for the
fleet's live cataloging payments (Node: @x402/fetch + @x402/evm 2.28.0
`ExactEvmScheme`, x402 protocol v2, `exact` scheme) — the same flow the
sibling token-verdict-mcp / route-verdict-mcp / list-smith-mcp packages
implement:

1. POST the route unpaid -> HTTP 402 with the challenge base64-encoded
   in the `payment-required` response header.
2. Parse the challenge, select the exact/eip155:8453 (Base) accepts
   entry, and check its amount against the spend guard for that route.
3. Build an EIP-3009 TransferWithAuthorization (from the signer, to the
   challenge's payTo, value = the challenge amount, validAfter 0,
   validBefore now + maxTimeoutSeconds, 32-byte random nonce), sign it
   as EIP-712 typed data against the USDC domain the challenge
   advertises (accepts.extra name/version, chainId 8453,
   verifyingContract = the asset).
4. Retry the POST with the payload base64-encoded in the
   `PAYMENT-SIGNATURE` header.
5. Read the settlement receipt from the `payment-response` header
   (exposed as `client.last_receipt`).

It stays synchronous on purpose: one request at a time is what an agent
debugging a payment wants, and httpx.MockTransport keeps the whole flow
testable without a network. Signing is local; the private key comes from
the operator's own environment and never leaves the machine.

This wrapper covers 20 of StallRow's priced routes from one host
(the inspector toolkit — sign-read, score-book, guard — included),
plus free reads (pack/pass balances and the Recheck feed v2). Each
route's spend guard
defaults to that route's known price (ROUTE_PRICES, mirroring the
service's own config); STALL_ROW_MAX_AMOUNT_UNITS can raise/lower the
ceiling globally, but the client never signs above the per-route guard
unless the operator explicitly raised the global ceiling past it.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

import httpx
from eth_account import Account
from eth_account.signers.local import LocalAccount

DEFAULT_BASE_URL = "https://stall-row-production.up.railway.app"

# Known per-route prices in USDC micro-units (mirror of the service's
# own PRICE_UNITS in QuincyGB/stall-row app/config.py). The spend guard
# never signs a challenge priced above the route's entry here (unless
# DEFAULT_MAX_AMOUNT_UNITS/global override says otherwise — see __init__).
ROUTE_PRICES: dict[str, int] = {
    "/v1/wallet-snapshot": 5_000,
    "/v1/allowance-audit": 5_000,
    "/v1/tx-decoder": 2_000,
    "/v1/gas-oracle": 1_000,
    "/v1/wallet-verdict": 20_000,
    "/v1/pre-trade-guard": 5_000,
    "/v1/tube-search": 10_000,
    "/v1/page-to-markdown": 10_000,
    "/v1/repo-vitals": 5_000,
    "/v1/price-check": 50_000,
    "/v1/card-smith": 100_000,
    "/v1/job-scout": 50_000,
    "/v1/recheck": 5_000,
    "/v1/index/changes": 100_000,
    "/v1/orchestrate": 750_000,
    "/v1/recheck/pack": 1_000_000,
    "/v1/fleet-pass": 5_000_000,
    "/v1/sign-read": 10_000,
    "/v1/score-book": 10_000,
    "/v1/guard": 10_000,
}

BASE_NETWORK = "eip155:8453"

TRANSFER_WITH_AUTHORIZATION_TYPES = {
    "TransferWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ]
}


class StallRowError(Exception):
    """Non-payment failure from the service (carries HTTP status + body)."""

    def __init__(self, status: int, detail: Any):
        super().__init__(f"stall row request failed: HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class PaymentRequiredError(Exception):
    """The endpoint demands payment and this client could/did not pay.

    Carries the full parsed 402 challenge so callers can inspect or
    settle it themselves. Raised when no signer is configured, when the
    price exceeds the spend guard, or when a paid retry still gets a 402.
    """

    def __init__(self, challenge: dict, reason: str):
        super().__init__(reason)
        self.challenge = challenge
        self.reason = reason

    def structured_error(self) -> dict:
        """The challenge as a JSON-friendly structured error object."""
        return {
            "error": "payment_required",
            "reason": self.reason,
            "x402Version": self.challenge.get("x402Version"),
            "resource": self.challenge.get("resource"),
            "accepts": self.challenge.get("accepts"),
            "extensions": self.challenge.get("extensions"),
            "hint": (
                "StallRow routes are priced per call in USDC on Base "
                "(x402) — see the route's accepts entry for the exact "
                "price. Configure AGENT_PRIVATE_KEY or AGENT_KEY_FILE "
                "with an EVM wallet holding USDC on Base and the call "
                "will be paid automatically, or settle this challenge "
                "with any x402 v2 client and retry."
            ),
        }


def parse_payment_required(response: httpx.Response) -> dict:
    """Parse the x402 v2 challenge from a 402 response.

    The challenge travels base64-encoded in the `payment-required`
    header; some stacks also put it in the JSON body. Raises ValueError
    when neither carries a usable challenge.
    """
    header = response.headers.get("payment-required")
    if header:
        try:
            decoded = json.loads(base64.b64decode(header).decode("utf-8"))
            if isinstance(decoded, dict) and decoded.get("accepts"):
                return decoded
        except (ValueError, UnicodeDecodeError):
            pass
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("accepts"):
        return body
    raise ValueError("402 response carried no parseable x402 payment challenge")


def select_requirement(
    challenge: dict, network: str = BASE_NETWORK, scheme: str = "exact"
) -> dict:
    """Pick the accepts entry this client can settle (exact EVM on Base)."""
    for entry in challenge.get("accepts") or []:
        if entry.get("scheme") == scheme and entry.get("network") == network:
            return entry
    raise ValueError(
        f"challenge offers no {scheme} requirement on {network}: "
        f"{[ (a.get('scheme'), a.get('network')) for a in challenge.get('accepts') or [] ]}"
    )


def build_authorization(
    from_address: str,
    requirement: dict,
    *,
    now: int | None = None,
    nonce: str | None = None,
) -> dict:
    """EIP-3009 authorization, string fields exactly as @x402/evm builds."""
    now = int(time.time()) if now is None else now
    return {
        "from": from_address,
        "to": requirement["payTo"],
        "value": str(requirement["amount"]),
        "validAfter": "0",
        "validBefore": str(now + int(requirement.get("maxTimeoutSeconds", 300))),
        "nonce": nonce or ("0x" + secrets.token_hex(32)),
    }


def sign_authorization(
    account: LocalAccount, requirement: dict, authorization: dict
) -> str:
    """EIP-712 sign the TransferWithAuthorization against the USDC domain
    the challenge advertises in accepts.extra (name/version)."""
    extra = requirement.get("extra") or {}
    if not extra.get("name") or not extra.get("version"):
        raise ValueError(
            "payment requirement is missing EIP-712 domain parameters "
            f"(extra.name/extra.version) for asset {requirement.get('asset')}"
        )
    network = requirement.get("network", BASE_NETWORK)
    if not network.startswith("eip155:"):
        raise ValueError(f"unsupported network for EVM signing: {network}")
    domain = {
        "name": extra["name"],
        "version": extra["version"],
        "chainId": int(network.split(":", 1)[1]),
        "verifyingContract": requirement["asset"],
    }
    message = {
        "from": authorization["from"],
        "to": authorization["to"],
        "value": int(authorization["value"]),
        "validAfter": int(authorization["validAfter"]),
        "validBefore": int(authorization["validBefore"]),
        "nonce": bytes.fromhex(authorization["nonce"].removeprefix("0x")),
    }
    signed = account.sign_typed_data(
        domain_data=domain,
        message_types=TRANSFER_WITH_AUTHORIZATION_TYPES,
        message_data=message,
    )
    return signed.signature.hex() if signed.signature.hex().startswith("0x") else "0x" + signed.signature.hex()


def build_payment_payload(
    challenge: dict, requirement: dict, authorization: dict, signature: str
) -> dict:
    """The x402 v2 PaymentPayload the retry carries."""
    return {
        "x402Version": challenge.get("x402Version", 2),
        "resource": challenge.get("resource"),
        "accepted": requirement,
        "payload": {"authorization": authorization, "signature": signature},
    }


def encode_payment_header(payload: dict) -> str:
    return base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


def decode_receipt(response: httpx.Response) -> dict | None:
    """Decode the base64 `payment-response` settlement receipt, if any."""
    raw = response.headers.get("payment-response")
    if not raw:
        return None
    try:
        return json.loads(base64.b64decode(raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def load_account_from_env(env: dict | None = None) -> LocalAccount | None:
    """Build the signer from the operator's OWN environment.

    AGENT_PRIVATE_KEY: a 0x-prefixed EVM private key.
    AGENT_KEY_FILE:    path to a file whose contents are that key.
    Returns None when neither is set — callers then never pay.
    """
    env = os.environ if env is None else env
    key = (env.get("AGENT_PRIVATE_KEY") or "").strip()
    if not key:
        key_file = (env.get("AGENT_KEY_FILE") or "").strip()
        if key_file:
            key = Path(key_file).read_text(encoding="utf-8").strip()
    if not key:
        return None
    if not key.startswith("0x"):
        key = "0x" + key
    return Account.from_key(key)


class StallRowClient:
    """Synchronous StallRow client with automatic x402 payment."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        account: LocalAccount | None = None,
        max_amount_units: int | None = None,
        timeout: float = 60.0,
        http: httpx.Client | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.account = account
        # Global ceiling: None means "each route's known price is its
        # own guard". A set value replaces the per-route guard for every
        # route (operator's explicit choice).
        self.max_amount_units = max_amount_units
        self._http = http or httpx.Client(timeout=timeout)
        self.last_receipt: dict | None = None

    # -- core flow ---------------------------------------------------------

    def _guard_for(self, path: str) -> int:
        if self.max_amount_units is not None:
            return self.max_amount_units
        return ROUTE_PRICES.get(path, 0)

    def _call_paid(self, path: str, body: dict) -> dict:
        """POST a priced route, paying the 402 challenge when a signer
        is configured. Returns the route's JSON (see schemas.py)."""
        url = self.base_url + path
        response = self._http.post(url, json=body)
        if response.status_code == 200:
            # Served without payment (e.g. a recheck covered by a Recheck
            # Pack balance, or a Fleet Pass debit) — take it.
            self.last_receipt = decode_receipt(response)
            return response.json()
        if response.status_code != 402:
            raise StallRowError(response.status_code, _safe_json(response))

        challenge = parse_payment_required(response)
        if self.account is None:
            raise PaymentRequiredError(
                challenge,
                "endpoint requires x402 payment and no signer is configured "
                "(set AGENT_PRIVATE_KEY or AGENT_KEY_FILE)",
            )
        requirement = select_requirement(challenge)
        cap = self._guard_for(path)
        if int(requirement["amount"]) > cap:
            raise PaymentRequiredError(
                challenge,
                f"price {requirement['amount']} exceeds spend guard {cap} "
                f"for {path} — refusing to sign",
            )
        authorization = build_authorization(self.account.address, requirement)
        signature = sign_authorization(self.account, requirement, authorization)
        payload = build_payment_payload(challenge, requirement, authorization, signature)
        paid = self._http.post(
            url,
            json=body,
            headers={"PAYMENT-SIGNATURE": encode_payment_header(payload)},
        )
        if paid.status_code == 402:
            raise PaymentRequiredError(
                parse_payment_required(paid),
                "paid retry still returned 402 (payment rejected or expired)",
            )
        if paid.status_code != 200:
            raise StallRowError(paid.status_code, _safe_json(paid))
        self.last_receipt = decode_receipt(paid)
        return paid.json()

    def _call_free(self, path: str, body: dict) -> dict:
        """POST one of the free balance reads (no payment flow)."""
        response = self._http.post(self.base_url + path, json=body)
        if response.status_code != 200:
            raise StallRowError(response.status_code, _safe_json(response))
        return response.json()

    # -- priced routes -----------------------------------------------------

    def wallet_snapshot(self, address: str) -> dict:
        return self._call_paid("/v1/wallet-snapshot", {"address": address})

    def allowance_audit(self, address: str) -> dict:
        return self._call_paid("/v1/allowance-audit", {"address": address})

    def tx_decoder(self, tx_hash: str) -> dict:
        return self._call_paid("/v1/tx-decoder", {"tx_hash": tx_hash})

    def gas_oracle(self) -> dict:
        return self._call_paid("/v1/gas-oracle", {})

    def wallet_verdict(self, address: str) -> dict:
        return self._call_paid("/v1/wallet-verdict", {"address": address})

    def pre_trade_guard(
        self,
        pay_to: str,
        amount: int | None = None,
        network: str | None = None,
        resource: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {"pay_to": pay_to}
        if amount is not None:
            body["amount"] = amount
        if network:
            body["network"] = network
        if resource:
            body["resource"] = resource
        return self._call_paid("/v1/pre-trade-guard", body)

    def tube_search(self, query: str, max_results: int | None = None) -> dict:
        body: dict[str, Any] = {"query": query}
        if max_results is not None:
            body["max_results"] = max_results
        return self._call_paid("/v1/tube-search", body)

    def page_to_markdown(self, url: str) -> dict:
        return self._call_paid("/v1/page-to-markdown", {"url": url})

    def repo_vitals(self, repo: str) -> dict:
        return self._call_paid("/v1/repo-vitals", {"repo": repo})

    def price_check(self, keywords: str, description: str | None = None) -> dict:
        body: dict[str, Any] = {"keywords": keywords}
        if description:
            body["description"] = description
        return self._call_paid("/v1/price-check", body)

    def card_smith(self, mode: str = "generate", **fields: Any) -> dict:
        body: dict[str, Any] = {"mode": mode}
        body.update({k: v for k, v in fields.items() if v is not None})
        return self._call_paid("/v1/card-smith", body)

    def job_scout(
        self, capabilities: list | None = None, limit: int | None = None
    ) -> dict:
        body: dict[str, Any] = {}
        if capabilities is not None:
            body["capabilities"] = capabilities
        if limit is not None:
            body["limit"] = limit
        return self._call_paid("/v1/job-scout", body)

    def recheck(
        self, url: str, watch_id: str | None = None, pack_id: str | None = None
    ) -> dict:
        body: dict[str, Any] = {"url": url}
        if watch_id:
            body["watch_id"] = watch_id
        if pack_id:
            body["pack_id"] = pack_id
        return self._call_paid("/v1/recheck", body)

    def index_changes(
        self,
        resource_contains: str | None = None,
        pay_to: str | None = None,
        service_name: str | None = None,
        limit: int | None = None,
    ) -> dict:
        body: dict[str, Any] = {}
        if resource_contains:
            body["resource_contains"] = resource_contains
        if pay_to:
            body["pay_to"] = pay_to
        if service_name:
            body["service_name"] = service_name
        if limit is not None:
            body["limit"] = limit
        return self._call_paid("/v1/index/changes", body)

    def orchestrate(
        self,
        address: str,
        service_url: str,
        listing_url: str,
        chain: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "address": address,
            "service_url": service_url,
            "listing_url": listing_url,
        }
        if chain:
            body["chain"] = chain
        return self._call_paid("/v1/orchestrate", body)

    def sign_read(
        self,
        url: str,
        declared_method: str | None = None,
        declared_amount: str | None = None,
        declared_pay_to: str | None = None,
        declared_network: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {"url": url}
        if declared_method:
            body["declared_method"] = declared_method
        if declared_amount:
            body["declared_amount"] = declared_amount
        if declared_pay_to:
            body["declared_pay_to"] = declared_pay_to
        if declared_network:
            body["declared_network"] = declared_network
        return self._call_paid("/v1/sign-read", body)

    def score_book(
        self,
        url: str | None = None,
        origin: str | None = None,
        pay_to: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {}
        if url:
            body["url"] = url
        if origin:
            body["origin"] = origin
        if pay_to:
            body["pay_to"] = pay_to
        return self._call_paid("/v1/score-book", body)

    def guard(self, url: str) -> dict:
        return self._call_paid("/v1/guard", {"url": url})

    def recheck_pack(self, url: str) -> dict:
        return self._call_paid("/v1/recheck/pack", {"url": url})

    def fleet_pass(self) -> dict:
        return self._call_paid("/v1/fleet-pass", {})

    # -- free reads ------------------------------------------------------------

    def recheck_feed(
        self,
        since_seq: int | None = None,
        watch_id: str | None = None,
        url: str | None = None,
        limit: int | None = None,
    ) -> dict:
        """Free read of the Recheck feed v2 cursor surface.

        GET /v1/recheck/feed?v=2 returns one JSON envelope per line
        (seq, record_id, schema_version, prev_hash, record_hash,
        record). since_seq=N is the gap-proof cursor mode: records
        with seq > N, ascending. The chain head rides in the
        X-Recheck-Feed-Latest-Seq / -Count response headers and is
        surfaced here so a poller can advance its cursor.
        """
        params: dict[str, Any] = {"v": "2"}
        if since_seq is not None:
            params["since_seq"] = since_seq
        if watch_id:
            params["watch_id"] = watch_id
        if url:
            params["url"] = url
        if limit is not None:
            params["limit"] = limit
        response = self._http.get(self.base_url + "/v1/recheck/feed", params=params)
        if response.status_code != 200:
            raise StallRowError(response.status_code, _safe_json(response))
        records = []
        for line in response.text.splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
        latest = response.headers.get("X-Recheck-Feed-Latest-Seq")
        count = response.headers.get("X-Recheck-Feed-Count")
        return {
            "format": "recheck-feed-v2",
            "count": len(records),
            "latest_seq": int(latest) if latest is not None else None,
            "feed_count": int(count) if count is not None else None,
            "records": records,
        }

    # -- free balance reads --------------------------------------------------

    def recheck_pack_balance(self, pack_id: str) -> dict:
        return self._call_free("/v1/recheck/pack-balance", {"pack_id": pack_id})

    def fleet_pass_balance(self, pass_id: str) -> dict:
        return self._call_free("/v1/fleet-pass/balance", {"pass_id": pass_id})


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text[:500]
