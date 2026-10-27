"""Server-level tests: every route is registered as a tool with the
service's input fields, and the schemas module covers all 17 priced
routes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from stall_row_mcp.client import ROUTE_PRICES
from stall_row_mcp.schemas import ROUTE_SCHEMAS
from stall_row_mcp.server import mcp, run_pre_trade_guard, run_wallet_verdict

FIXTURES = Path(__file__).parent / "fixtures"
VERDICT = json.loads((FIXTURES / "wallet_verdict_example.json").read_text())
GUARD = json.loads((FIXTURES / "pre_trade_guard_example.json").read_text())

EXPECTED_TOOLS = {
    "stall_wallet_snapshot", "stall_allowance_audit", "stall_tx_decoder",
    "stall_gas_oracle", "stall_wallet_verdict", "stall_pre_trade_guard",
    "stall_tube_search", "stall_page_to_markdown", "stall_repo_vitals",
    "stall_price_check", "stall_card_smith", "stall_job_scout",
    "stall_recheck", "stall_index_changes", "stall_orchestrate",
    "stall_recheck_pack", "stall_fleet_pass",
    "stall_recheck_pack_balance", "stall_fleet_pass_balance",
}


def _tools():
    tools = asyncio.run(mcp.list_tools())
    return {t.name: t for t in tools}


def test_all_tools_registered():
    assert set(_tools()) == EXPECTED_TOOLS


def test_tool_inputs_match_service_schemas():
    by_name = _tools()
    cases = {
        "stall_wallet_verdict": "/v1/wallet-verdict",
        "stall_pre_trade_guard": "/v1/pre-trade-guard",
        "stall_index_changes": "/v1/index/changes",
        "stall_orchestrate": "/v1/orchestrate",
    }
    for tool_name, path in cases.items():
        props = by_name[tool_name].inputSchema["properties"]
        service_props = ROUTE_SCHEMAS[path]["input"]["properties"]
        assert set(props) == set(service_props), tool_name
        assert by_name[tool_name].inputSchema.get("required", []) == \
            ROUTE_SCHEMAS[path]["input"]["required"], tool_name


def test_schemas_cover_every_priced_route():
    assert set(ROUTE_SCHEMAS) == set(ROUTE_PRICES)
    # The example output satisfies the service's required output keys.
    for path, example in (("/v1/wallet-verdict", VERDICT),
                          ("/v1/pre-trade-guard", GUARD)):
        for key in ROUTE_SCHEMAS[path]["output"]["required"]:
            assert key in example, (path, key)


def test_tool_call_via_run_happy_path():
    class FakeClient:
        def wallet_verdict(self, address):
            assert address.startswith("0x")
            return VERDICT

        def pre_trade_guard(self, pay_to, amount=None, network=None, resource=None):
            return GUARD

    assert run_wallet_verdict("0x" + "11" * 20, client=FakeClient())["verdict"] == "SAFE"
    result = run_pre_trade_guard("0x" + "11" * 20, client=FakeClient())
    assert result["verdict"] == "PROCEED"
    assert result["risk_score"] == 0
