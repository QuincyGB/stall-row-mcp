<!-- mcp-name: io.github.QuincyGB/stall-row-mcp -->

# stall-row-mcp

> **Standalone home:** [QuincyGB/stall-row-mcp](https://github.com/QuincyGB/stall-row-mcp)
> — sibling of the fleet's token-verdict-mcp, route-verdict-mcp,
> list-smith-mcp and lock-smith-mcp wrappers.

MCP server for **[StallRow](https://stall-row-production.up.railway.app)**
— the "Verdict Row" utility stall: 17 pay-per-call tools for agents,
sold over [x402](https://www.x402.org) in **USDC on Base**, no account,
no API key. Wallet snapshots and verdicts, a buyer-side Pre-Trade
Guard, web/YouTube/repo utilities, continuous re-checks of catalogued
x402 listings, the Verdict Index change feed, and the fleet's bulk
products (Recheck Packs, Fleet Pass, Orchestrate).

This package gives any MCP host (Claude Desktop, Cursor, …) native
tools that handle the full x402 v2 payment flow themselves — 402
challenge → EIP-3009 `TransferWithAuthorization` signature (USDC on
Base, EIP-712) → paid retry — using **your** agent wallet.

## Tools

| Tool | What it does | Price |
| --- | --- | --- |
| `stall_wallet_snapshot` | Balances, tx count, age, contract flag for one address | $0.005 |
| `stall_allowance_audit` | Outstanding ERC-20 approvals with unlimited-approval flags | $0.005 |
| `stall_tx_decoder` | Decode one Base tx hash into plain English | $0.002 |
| `stall_gas_oracle` | Current Base gas prices (slow / standard / fast) | $0.001 |
| `stall_wallet_verdict` | Wallet risk screen: SAFE / CAUTION / AVOID with reasons | $0.02 |
| `stall_pre_trade_guard` | Screen an x402 payTo *before* paying it: PROCEED / CAUTION / ABORT | $0.005 |
| `stall_tube_search` | YouTube search with transcripts | $0.01 |
| `stall_page_to_markdown` | Fetch a web page as clean markdown | $0.01 |
| `stall_repo_vitals` | GitHub repo health signals | $0.005 |
| `stall_price_check` | Pricing comps for an x402 listing from the live catalog | $0.05 |
| `stall_card_smith` | Generate or grade an agent card (agent-card.json) | $0.10 |
| `stall_job_scout` | Open agent-job digest filtered by capability | $0.05 |
| `stall_recheck` | Re-check one catalogued x402 URL (payability + drift) | $0.005 |
| `stall_index_changes` | Verdict Index change feed (new/changed/removed, rotations) | $0.10 |
| `stall_orchestrate` | Full due-diligence battery on one x402 endpoint | $0.75 |
| `stall_recheck_pack` | Buy a Recheck Pack — 250 checks for one endpoint | $1.00 |
| `stall_fleet_pass` | Buy a Fleet Pass — $5.00 prepaid fleet balance (90 days) | $5.00 |
| `stall_recheck_pack_balance` | Free balance read for a Recheck Pack | free |
| `stall_fleet_pass_balance` | Free status read for a Fleet Pass | free |
| `stall_sign_read` | Read one x402 endpoint's 402 sign: both wire formats parsed, exact decimals, declared-vs-live mismatches, Python/TS SDK verdicts | $0.01 |
| `stall_score_book` | Track record for one resource / origin / payTo from StallRow's own stores | $0.01 |
| `stall_guard` | Pre-pay verdict for one x402 endpoint (sign + bad-payTo registry + history): PROCEED / CAUTION / ABORT | $0.01 |
| `stall_recheck_feed` | Free read of the Recheck feed v2 — hash-chained envelopes with a `since_seq` cursor | free |

Verdicts are automated checks over public data — sanctions screens,
catalog history, chain reads — with the evidence attached. A flag is a
lead to review, not proof of wrongdoing, and no verdict is a guarantee.

## Install

```bash
pip install stall-row-mcp
# or run without installing:
uvx stall-row-mcp
```

## Configuration

All configuration is environment variables — set them on the MCP server
process in your host's config. Your key never leaves your machine; the
service only ever sees a signed payment authorization.

| Variable | Required | Meaning |
| --- | --- | --- |
| `AGENT_PRIVATE_KEY` | for paid calls | EVM private key (`0x…`) of **your** wallet holding USDC on Base. Used locally to sign the x402 payment. |
| `AGENT_KEY_FILE` | alternative | Path to a file containing that key instead. |
| `STALL_ROW_URL` | no | Service base URL override (default: production). |
| `STALL_ROW_MAX_AMOUNT_UNITS` | no | Optional global spend-guard ceiling per call, USDC micro-units. Default: each route's known price is its own guard. |

**Without a signer the tools never pay.** They return the parsed 402
challenge as a structured error — `{"error": "payment_required",
"accepts": […], …}` — so the calling agent can see the exact price,
asset, network and `payTo` and decide what to do. With a signer,
charges above the spend guard are likewise refused before signing.

## MCP host config

```json
{
  "mcpServers": {
    "stall-row": {
      "command": "uvx",
      "args": ["stall-row-mcp"],
      "env": {
        "AGENT_PRIVATE_KEY": "0xYOUR_AGENT_WALLET_KEY"
      }
    }
  }
}
```

## The service being wrapped

```
POST https://stall-row-production.up.railway.app/v1/pre-trade-guard
{"pay_to": "0x…", "resource": "https://seller.example/v1/thing"}
→ 200 {"verdict": "PROCEED", "risk_score": 10, "reasons": [...],
       "evidence": {...}}
```

The schemas in `src/stall_row_mcp/schemas.py` are copied verbatim from
the service's own discovery document (`/.well-known/x402`) — the
published contract, not a paraphrase.

## Development

```bash
pip install -e ".[dev]"
pytest
```

Tests mock the HTTP layer and use a throwaway signer generated at test
time — no network, no real money, no key material in the repo. The 402
fixtures in `tests/fixtures/` are real: captured from unpaid probes of
production on 2026-10-27.

## License

MIT
