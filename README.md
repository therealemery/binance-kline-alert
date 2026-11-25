# binance-kline-alert

[![CI](https://github.com/therealemery/binance-kline-alert/actions/workflows/ci.yml/badge.svg)](https://github.com/therealemery/binance-kline-alert/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

English | [简体中文](./README.zh-CN.md)

A **Binance K-line pattern monitor & natural-language alert bot** built for **WeCom (Enterprise WeChat)** internal groups — powered by Binance public Spot APIs (no Binance API key required) and an LLM rule compiler (default: `deepseek-flash`, compatible with any OpenAI-format endpoint).

Supports both **Binance tokenized US stocks (bStocks, e.g., `NVDABUSDT`, `TSLABUSDT`, `AAPLBUSDT`)** and **Spot crypto pairs (`BTCUSDT`, `ETHUSDT`, `SOLUSDT`, etc.)**. Team members don't need to memorize commands — just `@bot` in the WeCom group using natural language to check candles, manage watchlists, or create custom technical rules backed by a deterministic AST engine and historical backtest preview.

---

## Highlights

- **LLM-Compiled Deterministic Rules (Zero Ongoing LLM Cost)**
  - The LLM is only invoked when creating a new rule or routing free-form chat. Routine K-line polling is executed 100% locally by a strict Python **AST whitelist evaluator** (no `eval()`, zero hallucination, millisecond latency, zero recurring LLM cost).
  - Built-in candlestick patterns & indicators: `bullish_engulfing`, `bearish_engulfing`, `hammer`, `shooting_star`, `inside_bar`, `three_white_soldiers`, `three_black_crows`, plus `ma`, `ema`, `vol_ma`, `rsi`, `highest_high`, `lowest_low`, and arbitrary multi-candle comparisons.
  - Before activating any new rule, the bot automatically backtests it across the latest **120 closed candles** and replies with hit count + latest trigger timestamp for confirmation.
- **Zero-Learning-Curve Natural Language Group Interaction**
  - Supports multi-intent requests in a single message and automatic ticker resolution (e.g., `"Remove Apple, add Microsoft, and check NVIDIA and BTC right now"`).
  - Context-aware follow-ups for confirming/canceling pending rule drafts and enabling/disabling rules by ID or description.
- **Dual-Channel WeCom Integration (No Public Domain Required)**
  - **Group Webhook (Primary Alert Delivery)**: Permanent, unaffected by 24-hour session expiration limits, with built-in sliding-window rate limiting (18 msgs/min) to stay safely under WeCom's 20 msgs/min cap.
  - **WeCom Smart Bot WebSocket (`wecom-aibot-sdk`)**: Connects outbound to `wss://openws.work.weixin.qq.com` for bidirectional group `@mention` commands — no public IP, reverse proxy, or HTTPS callback server required.
  - Both channels are independently toggleable (Webhook-only, Combo mode, or Smart-Bot-only).
- **Strict Closed-Candle Evaluation & SQLite Deduplication**
  - Fetches `count + 1` candles and filters by `closeTime <= now - 1s` so still-forming candles never cause intra-bar false alarms.
  - Persists watchlists, custom rules, per-`(rule, symbol, interval)` deduplication state, and trigger history in SQLite. Failed webhook deliveries are not marked as sent and automatically retry on the next cycle.

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
│  WeCom Smart Bot    │◀────▶│  Command & Monitor  │─────▶│ WeCom Group Webhook  │
│ (WebSocket Client)  │      │  + Safe AST Engine  │      │ (24/7 Alert Delivery)│
└─────────────────────┘      └──────────┬──────────┘      └──────────────────────┘
                                        │ Invoked only for NL chat / rule creation
                                        ▼
                             ┌─────────────────────┐      ┌──────────────────────┐
                             │ DeepSeek-Flash LLM  │      │   SQLite State DB    │
                             │(Intent + DSL Comp.) │      │(Symbols/Rules/Dedup) │
                             └─────────────────────┘      └──────────────────────┘
```

---

## Quick Start

### 1. Clone & Install

Requires Python 3.10+:

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

Edit `.env`:

```ini
# Optional: DeepSeek API key for natural-language intent routing & rule compilation
# (Works with any OpenAI-compatible Chat Completions endpoint)
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash

# Recommended: WeCom internal group Webhook URL for reliable 24/7 alert delivery
WECOM_WEBHOOK_URL=

# Optional: WeCom Smart Bot long-connection credentials (WeCom Admin -> Smart Bot -> API Mode)
WECOM_BOT_ID=
WECOM_BOT_SECRET=

# Optional: HTTP/SOCKS5 proxy for Binance API (leave empty on non-restricted VPS regions)
BINANCE_PROXY=
```

### 3. Verify & Run

```bash
# 1) Validate configured symbols against Binance Spot exchangeInfo
binance-alert -c config.yaml validate-symbols

# 2) Dry-run a single poll cycle (prints signals without pushing to WeCom)
binance-alert -c config.yaml check-once

# 3) Test LLM rule compilation + live 120-candle Binance backtest (add --save to persist)
binance-alert -c config.yaml test-rule "NVDA and BTC 1h bullish engulfing with 1.5x volume"

# 4) Send a connectivity test card to your WeCom group webhook
binance-alert -c config.yaml test-webhook

# 5) Start the continuous monitor + WeCom Smart Bot
binance-alert -c config.yaml run
```

---

## Group Interaction Examples

Once the Smart Bot is added to your WeCom internal group, simply `@bot` in plain language (Chinese or English):

- **Status & Live Check**: `"What symbols are we watching? Also check NVDA and BTC right now"`
- **Watchlist Management**: `"Add MSFT and DOGE"`, `"Remove AAPL"`
- **Create Custom Rule**: `"Alert me when ETH 15m forms a hammer candle above MA20"` → review the 120-candle backtest preview → reply `"confirm"`
- **Manage Rules & Alerts**: `"Show all rules"`, `"Disable rule #1"`, `"Delete rule #2"`, `"Pause alerts"`, `"Resume alerts"`

Fast-path keyword commands are also supported (0 ms, no LLM call): `status`, `list`, `add`, `remove`, `check`, `rules`, `history`, `pause`, `resume`, `help`.

---

## Production Deployment

Recommended on a lightweight VPS in **Singapore, Tokyo, or Hong Kong** (1 vCPU / 1 GB RAM is plenty; resident memory is ~50–80 MB, with direct low-latency connectivity to both Binance and WeCom. Avoid US regions due to Binance HTTP 451 geo-restrictions).

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

> **Note:** WeCom Smart Bot allows only **1 active WebSocket long-connection** per `BotID` at a time. Avoid running multiple instances with the same `WECOM_BOT_ID` / `WECOM_BOT_SECRET` simultaneously.

---

## Development & Tests

```bash
pytest -v
```

## Disclaimer

This project is intended solely for public market data monitoring and technical learning. It does **not** constitute financial or investment advice and does **not** execute trades or handle funds. Users are responsible for complying with applicable local regulations and exchange/platform terms of service.

## License

[MIT](./LICENSE)
