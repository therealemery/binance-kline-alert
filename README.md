# binance-kline-alert

[![CI](https://github.com/therealemery/binance-kline-alert/actions/workflows/ci.yml/badge.svg)](https://github.com/therealemery/binance-kline-alert/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

English | [简体中文](./README.zh-CN.md)

A **Binance K-line pattern monitor & natural-language alert bot** supporting both **Telegram** and **WeCom (Enterprise WeChat)** — powered by Binance public Spot APIs (no Binance API key required) and an LLM rule compiler (default: `deepseek-flash`, compatible with any OpenAI-format endpoint).

Monitors both **Binance tokenized US stocks (bStocks, e.g., `NVDABUSDT`, `TSLABUSDT`, `AAPLBUSDT`)** and **Spot crypto pairs (`BTCUSDT`, `ETHUSDT`, `SOLUSDT`, etc.)**. Team members don't need to memorize commands — just message or `@bot` in Telegram or WeCom using plain natural language to check candles, manage watchlists, or create custom technical rules backed by a deterministic AST engine and 120-candle backtest preview.

---

## Highlights

- **LLM-Compiled Deterministic Rules (Zero Ongoing LLM Cost)**
  - The LLM is only invoked when creating a new rule or routing free-form chat. Routine K-line polling is executed 100% locally by a strict Python **AST whitelist evaluator** (no `eval()`, zero hallucination, millisecond latency, zero recurring LLM cost).
  - Built-in candlestick patterns & indicators: `bullish_engulfing`, `bearish_engulfing`, `hammer`, `shooting_star`, `inside_bar`, `three_white_soldiers`, `three_black_crows`, plus `ma`, `ema`, `vol_ma`, `rsi`, `highest_high`, `lowest_low`, and arbitrary multi-candle comparisons.
  - Before activating any new rule, the bot automatically backtests it across the latest **120 closed candles** and replies with hit count + latest trigger timestamp for confirmation.
- **Zero-Learning-Curve Natural Language Interaction**
  - Supports multi-intent requests in a single message and automatic ticker resolution (e.g., `"Remove Apple, add Microsoft, and check NVIDIA and BTC right now"`).
  - Context-aware follow-ups for confirming/canceling pending rule drafts and enabling/disabling rules by ID or description.
- **Multi-Channel Delivery: Telegram + WeCom (No Public Domain Required)**
  - **Telegram Bot (Zero Extra Dependencies)**: Built directly on `httpx` via `getUpdates` long polling + `sendMessage` (`parse_mode="HTML"` with automatic plain-text fallback on 400 and `retry_after` handling on 429). Supports DMs, group `@mentions`, `/slash@bot` commands, and direct replies to bot messages.
  - **WeCom Group Webhook + Smart Bot WebSocket (`wecom-aibot-sdk`)**: Permanent 24/7 webhook alert delivery (18 msgs/min sliding-window limiter) paired with outbound WebSocket long-connection to `wss://openws.work.weixin.qq.com`.
  - Run **Telegram-only**, **WeCom-only**, or **both in parallel** with isolated per-channel `chat_ids` and `allowed_users` authorization.
- **Strict Closed-Candle Evaluation & SQLite Deduplication**
  - Fetches `count + 1` candles and filters by `closeTime <= now - 1s` so still-forming candles never cause intra-bar false alarms.
  - Persists watchlists, custom rules, per-`(rule, symbol, interval)` deduplication state, and trigger history in SQLite. Failed deliveries are not marked as sent and automatically retry on the next cycle.

---

## Architecture

```text
                       ┌──────────────────────────────┐
                       │   Binance Public Spot API    │  (No API key required)
                       │ /api/v3/klines & exchangeInfo│
                       └──────────────┬───────────────┘
                                      │ Polls every 45s (closed candles only)
                                      ▼
┌─────────────────────┐      ┌─────────────────────┐      ┌──────────────────────┐
│   Telegram Bot      │◀────▶│  Command & Monitor  │─────▶│  Telegram / WeCom    │
│ (getUpdates Polling)│      │  + Safe AST Engine  │      │  24/7 Alert Delivery │
│  WeCom Smart Bot    │◀────▶│                     │      └──────────────────────┘
│ (WebSocket Client)  │      └──────────┬──────────┘
└─────────────────────┘                 │ Invoked only for NL chat / rule creation
                                        ▼
                             ┌─────────────────────┐      ┌──────────────────────┐
                             │ DeepSeek-Flash LLM  │      │   SQLite State DB    │
                             │(Intent + DSL Comp.) │      │(Symbols/Rules/Dedup) │
                             └─────────────────────┘      └──────────────────────┘
```

---

## Quick Start

### 1. Clone & Install

Requires Python 3.11+:

```bash
git clone https://github.com/therealemery/binance-kline-alert.git
cd binance-kline-alert

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 2. Configure Environment & YAML

```bash
cp config.example.yaml config.yaml
cp .env.example .env
```

Edit `.env` (configure Telegram, WeCom, or both):

```ini
# Optional: DeepSeek API key for natural-language intent routing & rule compilation
# (Works with any OpenAI-compatible Chat Completions endpoint)
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash

# Telegram Bot (get token from @BotFather -> /newbot)
TELEGRAM_BOT_TOKEN=
# Comma-separated target chat_ids (group IDs usually start with -100...)
TELEGRAM_CHAT_IDS=
TELEGRAM_PROXY=

# WeCom (Enterprise WeChat) - optional if using Telegram only
WECOM_WEBHOOK_URL=
WECOM_BOT_ID=
WECOM_BOT_SECRET=

# Optional: HTTP/SOCKS5 proxy for Binance API (leave empty on non-restricted VPS regions)
BINANCE_PROXY=
```

> **Telegram Group Setup Tip**: Add your bot to the group. With Telegram's default privacy mode (`require_mention_in_group: true`), users can trigger it via `@your_bot ...`, `/status`, or by **replying directly to any bot message** (e.g., replying `confirm` to a rule preview). To let the bot read all group messages without `@mention`, disable privacy mode via `@BotFather` → `/setprivacy` → `Disable`.

### 3. Verify & Run

```bash
# 1) Validate configured symbols against Binance Spot exchangeInfo
binance-alert -c config.yaml validate-symbols

# 2) Dry-run a single poll cycle (prints signals without pushing)
binance-alert -c config.yaml check-once

# 3) Test LLM rule compilation + live 120-candle Binance backtest (add --save to persist)
binance-alert -c config.yaml test-rule "NVDA and BTC 1h bullish engulfing with 1.5x volume"

# 4) Send a connectivity test card to Telegram and/or WeCom
binance-alert -c config.yaml test-telegram
binance-alert -c config.yaml test-webhook

# 5) Start the continuous monitor + bots
binance-alert -c config.yaml run
```

---

## Interactive Examples (Telegram & WeCom)

In a Telegram DM, Telegram group (`@bot` / `/cmd` / reply), or WeCom group (`@bot`):

- **Status & Live Check**: `/status`, `"What symbols are we watching? Also check NVDA and BTC right now"`
- **Watchlist Management**: `"Add MSFT and DOGE"`, `"Remove AAPL"`
- **Create Custom Rule**: `"Alert me when ETH 15m forms a hammer candle above MA20"` → review the 120-candle backtest preview → reply `"confirm"`
- **Manage Rules & Alerts**: `/rules`, `"Disable rule #1"`, `"Delete rule #2"`, `/pause`, `/resume`

Fast-path commands work with zero LLM calls: `status`, `list`, `add`, `remove`, `check`, `rules`, `history`, `pause`, `resume`, `help` (with or without leading `/` or `@botname`).

---

## Production Deployment

Recommended on a lightweight VPS in **Singapore, Tokyo, or Hong Kong** (1 vCPU / 1 GB RAM is plenty; resident memory is ~50–80 MB, with direct low-latency connectivity to Binance, Telegram, and WeCom. Avoid US regions due to Binance HTTP 451 geo-restrictions).

### Option 1: systemd (Lightweight, Recommended)

```bash
sudo mkdir -p /opt/binance-kline-alert/data
# Copy project files along with config.yaml and .env into /opt/binance-kline-alert
chmod 600 /opt/binance-kline-alert/.env

python3 -m venv /opt/binance-kline-alert/.venv
/opt/binance-kline-alert/.venv/bin/pip install /opt/binance-kline-alert

# Adjust User/Group in deploy/binance-kline-alert.service if needed
sudo cp deploy/binance-kline-alert.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now binance-kline-alert
journalctl -u binance-kline-alert -f
```

### Option 2: Docker Compose

```bash
docker compose up -d --build
docker compose logs -f
```

> **Note:** Both Telegram `getUpdates` polling and WeCom Smart Bot WebSocket allow only **1 active instance** per bot token / `BotID`. Do not run two instances simultaneously with the same credentials.

---

## Development & Tests

```bash
pytest -v
```

## Disclaimer

This project is intended solely for public market data monitoring and technical learning. It does **not** constitute financial or investment advice and does **not** execute trades or handle funds. Users are responsible for complying with applicable local regulations and exchange/platform terms of service.

## License

[MIT](./LICENSE)
