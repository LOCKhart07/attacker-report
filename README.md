# Attacker Report

Monitors Gnosis Chain for oracle manipulation on Omen prediction markets (Reality.eth).

## What it does

Two polling loops running concurrently:

- **Market monitor** (every 30m) — checks all unfinalized Pearl/QS markets for answers from unknown addresses. Cross-references betting activity on the same market.
- **Suspect monitor** (every 5m) — tracks all on-chain activity from a known attacker address. Summarizes new transactions with Grok AI.

Alerts are sent to Telegram.

## Setup

```bash
cp .env.example .env
# fill in SUBGRAPH_API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_IDS
uv run python main.py
```

## Docker

```bash
docker compose up -d
```

## Config

All configurable via `.env` — see `.env.example` for all options.
