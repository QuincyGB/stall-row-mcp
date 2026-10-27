"""MCP server (stdio) exposing StallRow's routes as tools.

Run it with an MCP host (Claude Desktop, Cursor, any MCP client):

    stall-row-mcp            # after pip install
    python -m stall_row_mcp  # from a checkout

Configuration is entirely from the environment of whoever runs it:

    AGENT_PRIVATE_KEY   EVM private key (0x...) of YOUR wallet holding
                        USDC on Base — used to pay the per-call x402
                        charges automatically. Never shared with the
                        tool author; the key stays on your machine.
    AGENT_KEY_FILE      Alternative: path to a file containing the key.
    STALL_ROW_URL       Override the service base URL
                        (default: production).
    STALL_ROW_MAX_AMOUNT_UNITS
                        Spend-guard ceiling per call, in USDC
                        micro-units. Default: each route's known price
                        is its own guard. Setting this replaces the
                        per-route guards with one global ceiling.

With no signer configured the tools never pay: they return the parsed
402 challenge as a structured error so the calling agent can see the
price, asset, network and payTo and decide what to do.
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import (
    DEFAULT_BASE_URL,
    PaymentRequiredError,
    StallRowClient,
    StallRowError,
    load_account_from_env,
)

mcp = FastMCP("stall-row")


def build_client(env: dict | None = None) -> StallRowClient:
    env = os.environ if env is None else env
    max_units_raw = (env.get("STALL_ROW_MAX_AMOUNT_UNITS") or "").strip()
    return StallRowClient(
        base_url=(env.get("STALL_ROW_URL") or DEFAULT_BASE_URL),
        account=load_account_from_env(env),
        max_amount_units=int(max_units_raw) if max_units_raw else None,
    )


def _wrap(fn, client: StallRowClient | None) -> dict[str, Any]:
    """Run one client call, translating payment/upstream failures into
    the structured error dicts MCP hosts can reason about."""
    client = client or build_client()
    try:
        return fn(client)
    except PaymentRequiredError as exc:
        return exc.structured_error()
    except StallRowError as exc:
        return {"error": "upstream_error", "status": exc.status, "detail": exc.detail}


# --- transport-free runners (tests drive these with a mock client) ---------


def run_wallet_snapshot(address: str, client: StallRowClient | None = None) -> dict:
    return _wrap(lambda c: c.wallet_snapshot(address), client)


def run_allowance_audit(address: str, client: StallRowClient | None = None) -> dict:
    return _wrap(lambda c: c.allowance_audit(address), client)


def run_tx_decoder(tx_hash: str, client: StallRowClient | None = None) -> dict:
    return _wrap(lambda c: c.tx_decoder(tx_hash), client)


def run_gas_oracle(client: StallRowClient | None = None) -> dict:
    return _wrap(lambda c: c.gas_oracle(), client)


def run_wallet_verdict(address: str, client: StallRowClient | None = None) -> dict:
    return _wrap(lambda c: c.wallet_verdict(address), client)


def run_pre_trade_guard(
    pay_to: str,
    amount: int | None = None,
    network: str | None = None,
    resource: str | None = None,
    client: StallRowClient | None = None,
) -> dict:
    return _wrap(lambda c: c.pre_trade_guard(pay_to, amount, network, resource), client)


def run_tube_search(
    query: str, max_results: int | None = None, client: StallRowClient | None = None
) -> dict:
    return _wrap(lambda c: c.tube_search(query, max_results), client)


def run_page_to_markdown(url: str, client: StallRowClient | None = None) -> dict:
    return _wrap(lambda c: c.page_to_markdown(url), client)


def run_repo_vitals(repo: str, client: StallRowClient | None = None) -> dict:
    return _wrap(lambda c: c.repo_vitals(repo), client)


def run_price_check(
    keywords: str, description: str | None = None, client: StallRowClient | None = None
) -> dict:
    return _wrap(lambda c: c.price_check(keywords, description), client)


def run_card_smith(
    mode: str = "generate", client: StallRowClient | None = None, **fields: Any
) -> dict:
    return _wrap(lambda c: c.card_smith(mode, **fields), client)


def run_job_scout(
    capabilities: list | None = None,
    limit: int | None = None,
    client: StallRowClient | None = None,
) -> dict:
    return _wrap(lambda c: c.job_scout(capabilities, limit), client)


def run_recheck(
    url: str,
    watch_id: str | None = None,
    pack_id: str | None = None,
    client: StallRowClient | None = None,
) -> dict:
    return _wrap(lambda c: c.recheck(url, watch_id, pack_id), client)


def run_index_changes(
    resource_contains: str | None = None,
    pay_to: str | None = None,
    service_name: str | None = None,
    limit: int | None = None,
    client: StallRowClient | None = None,
) -> dict:
    return _wrap(
        lambda c: c.index_changes(resource_contains, pay_to, service_name, limit),
        client,
    )


def run_orchestrate(
    address: str,
    service_url: str,
    listing_url: str,
    chain: str | None = None,
    client: StallRowClient | None = None,
) -> dict:
    return _wrap(
        lambda c: c.orchestrate(address, service_url, listing_url, chain), client
    )


def run_recheck_pack(url: str, client: StallRowClient | None = None) -> dict:
    return _wrap(lambda c: c.recheck_pack(url), client)


def run_fleet_pass(client: StallRowClient | None = None) -> dict:
    return _wrap(lambda c: c.fleet_pass(), client)


def run_recheck_pack_balance(pack_id: str, client: StallRowClient | None = None) -> dict:
    return _wrap(lambda c: c.recheck_pack_balance(pack_id), client)


def run_fleet_pass_balance(pass_id: str, client: StallRowClient | None = None) -> dict:
    return _wrap(lambda c: c.fleet_pass_balance(pass_id), client)


# --- MCP tools ---------------------------------------------------------------


@mcp.tool()
def stall_wallet_snapshot(address: str) -> dict[str, Any]:
    """Wallet snapshot: balances, tx count, age, contract flag ($0.005).

    Calls StallRow POST /v1/wallet-snapshot. Returns token balances, tx
    count, first-activity age and contract detection for one address.
    """
    return run_wallet_snapshot(address)


@mcp.tool()
def stall_allowance_audit(address: str) -> dict[str, Any]:
    """Allowance audit: outstanding ERC-20 approvals and risk ($0.005).

    Calls StallRow POST /v1/allowance-audit. Returns every live token
    allowance for the address with unlimited-approval flags.
    """
    return run_allowance_audit(address)


@mcp.tool()
def stall_tx_decoder(tx_hash: str) -> dict[str, Any]:
    """Decode one Base transaction hash into plain English ($0.002).

    Calls StallRow POST /v1/tx-decoder.
    """
    return run_tx_decoder(tx_hash)


@mcp.tool()
def stall_gas_oracle() -> dict[str, Any]:
    """Current Base gas prices, slow/standard/fast ($0.001).

    Calls StallRow POST /v1/gas-oracle.
    """
    return run_gas_oracle()


@mcp.tool()
def stall_wallet_verdict(address: str) -> dict[str, Any]:
    """Risk screen for one wallet: SAFE / CAUTION / AVOID ($0.02).

    Calls StallRow POST /v1/wallet-verdict. Combines a sanctions screen,
    wallet age/activity and catalog track record into a scored verdict
    with named reasons. Screens and adverse-media flags are automated
    checks — a match is a lead to review, not proof of wrongdoing.
    """
    return run_wallet_verdict(address)


@mcp.tool()
def stall_pre_trade_guard(
    pay_to: str,
    amount: int | None = None,
    network: str | None = None,
    resource: str | None = None,
) -> dict[str, Any]:
    """Screen an x402 payTo BEFORE paying it ($0.005).

    Calls StallRow POST /v1/pre-trade-guard. Combines the Verdict Index
    listing history (first-seen, payTo rotations), a sanctions screen
    and wallet facts into a PROCEED / CAUTION / ABORT verdict with
    reasons. Hard-aborts on a sanctions match or when the named
    resource's catalogued payTo differs from the one presented. Legs
    that could not be evaluated cap the verdict at CAUTION and say so.

    Args:
        pay_to: The 0x address about to be paid.
        amount: Optional amount in USDC micro-units about to be paid.
        network: Optional CAIP-2 network (default eip155:8453).
        resource: Optional URL of the x402 resource being bought.
    """
    return run_pre_trade_guard(pay_to, amount, network, resource)


@mcp.tool()
def stall_tube_search(query: str, max_results: int | None = None) -> dict[str, Any]:
    """Search YouTube; structured results with transcripts ($0.01).

    Calls StallRow POST /v1/tube-search.
    """
    return run_tube_search(query, max_results)


@mcp.tool()
def stall_page_to_markdown(url: str) -> dict[str, Any]:
    """Fetch a web page as clean markdown ($0.01).

    Calls StallRow POST /v1/page-to-markdown.
    """
    return run_page_to_markdown(url)


@mcp.tool()
def stall_repo_vitals(repo: str) -> dict[str, Any]:
    """GitHub repo health: stars, activity, maintenance signals ($0.005).

    Calls StallRow POST /v1/repo-vitals. repo is "owner/name" or a URL.
    """
    return run_repo_vitals(repo)


@mcp.tool()
def stall_price_check(keywords: str, description: str | None = None) -> dict[str, Any]:
    """Pricing comps for an x402 listing ($0.05).

    Calls StallRow POST /v1/price-check: matches your keywords against
    the live CDP Bazaar catalog and returns the price distribution,
    closest comparables and a recommended listing price with rationale.
    """
    return run_price_check(keywords, description)


@mcp.tool()
def stall_card_smith(
    mode: str = "generate",
    name: str | None = None,
    description: str | None = None,
    skills: list[str] | None = None,
    card: dict | None = None,
) -> dict[str, Any]:
    """Generate or grade an agent card ($0.10).

    Calls StallRow POST /v1/card-smith. mode "generate" builds a
    structurally valid /.well-known/agent-card.json from your fields;
    mode "grade" scores a supplied card (pass it as `card`) and
    itemizes its defects.
    """
    return run_card_smith(mode=mode, name=name, description=description,
                          skills=skills, card=card)


@mcp.tool()
def stall_job_scout(
    capabilities: list[str] | None = None, limit: int | None = None
) -> dict[str, Any]:
    """Digest of open agent jobs, filtered by capability ($0.05).

    Calls StallRow POST /v1/job-scout: currently open agent jobs from
    the marketplaces the fleet sweeps daily, sorted by budget, with a
    stale flag when the snapshot ages past 24 hours.
    """
    return run_job_scout(capabilities, limit)


@mcp.tool()
def stall_recheck(
    url: str, watch_id: str | None = None, pack_id: str | None = None
) -> dict[str, Any]:
    """Re-check one catalogued x402 URL: payability + drift ($0.005).

    Calls StallRow POST /v1/recheck. With a pack_id that still has
    balance, the call is served from the pack and no payment is made;
    otherwise it is $0.005 per check.
    """
    return run_recheck(url, watch_id, pack_id)


@mcp.tool()
def stall_index_changes(
    resource_contains: str | None = None,
    pay_to: str | None = None,
    service_name: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Verdict Index: the CDP Bazaar catalog changelog ($0.10).

    Calls StallRow POST /v1/index/changes: the diff between the two
    newest full-catalog snapshots (price, asset, network, payTo,
    description, service name), with added/removed resources counted.
    Snapshots refresh about every 6 hours. Optional filters:
    resource_contains, pay_to, service_name, limit.
    """
    return run_index_changes(resource_contains, pay_to, service_name, limit)


@mcp.tool()
def stall_orchestrate(
    address: str,
    service_url: str,
    listing_url: str,
    chain: str | None = None,
) -> dict[str, Any]:
    """One combined fleet report ($0.75).

    Calls StallRow POST /v1/orchestrate: Token Verdict's verdict on a
    token (address + chain), RouteVerdict's delivery check and
    LockSmith's security audit summary on service_url, and ListSmith's
    grade on listing_url — four fleet services in one settled call. A
    failed leg is marked in the report, not hidden.
    """
    return run_orchestrate(address, service_url, listing_url, chain)


@mcp.tool()
def stall_recheck_pack(url: str) -> dict[str, Any]:
    """Buy a Recheck Pack: 250 change-checks for ONE endpoint ($1.00).

    Calls StallRow POST /v1/recheck/pack. The pack's credits may only
    check the endpoint named by `url`; the pack is good for 90 days.
    The returned pack_id is the bearer credential — store it like a key.
    """
    return run_recheck_pack(url)


@mcp.tool()
def stall_fleet_pass() -> dict[str, Any]:
    """Buy a Fleet Pass: a $5.00 prepaid fleet balance (90 days).

    Calls StallRow POST /v1/fleet-pass. The $5.00 balance spends on
    any paid fleet route, debited at that route's listed price. The
    returned pass_id is the bearer credential — store it like a key.
    """
    return run_fleet_pass()


@mcp.tool()
def stall_recheck_pack_balance(pack_id: str) -> dict[str, Any]:
    """Free: remaining balance of a Recheck Pack.

    Calls StallRow POST /v1/recheck/pack-balance (no charge).
    """
    return run_recheck_pack_balance(pack_id)


@mcp.tool()
def stall_fleet_pass_balance(pass_id: str) -> dict[str, Any]:
    """Free: status and remaining term of a Fleet Pass.

    Calls StallRow POST /v1/fleet-pass/balance (no charge).
    """
    return run_fleet_pass_balance(pass_id)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
