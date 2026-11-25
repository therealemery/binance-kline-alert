# binance-kline-alert

[![CI](https://github.com/therealemery/binance-kline-alert/actions/workflows/ci.yml/badge.svg)](https://github.com/therealemery/binance-kline-alert/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

[English](./README.md) | 简体中文

基于币安公开 Spot API（免 API Key）的 **K 线形态监控 + 自然语言规则配置 Bot**，原生同时支持 **Telegram Bot** 与 **企业微信内部群**（可纯 Telegram、纯企微或双通道并行，无需公网域名）。

同时支持 **币安美股代币（bStocks，如 `NVDABUSDT`、`TSLABUSDT`、`AAPLBUSDT`）** 与 **主流加密现货（如 `BTCUSDT`、`ETHUSDT`、`SOLUSDT`）**，成员无需记忆命令，直接用自然语言即可查行情、增删标的，并通过 LLM（默认 `deepseek-flash`，兼容任意 OpenAI 格式接口）将自然语言编译为**安全确定性公式 + 120 根 K 线历史回测预览**。

---

## 核心亮点

- **LLM 编译为确定性规则 + 本地零成本运行**
  - 仅在用户新增规则或自然语言对话时调用 LLM；日常行情轮询 100% 由本地 Python AST 白名单引擎执行，**零 LLM 成本、毫秒级、无幻觉、禁止 `eval` 任意代码**。
  - 内置经典形态与指标：`bullish_engulfing`（阳包阴）、`bearish_engulfing`（阴包阳）、`hammer`（锤头线）、`shooting_star`（流星线）、`inside_bar`（孕线）、`three_white_soldiers`、`three_black_crows`，以及 `ma`、`ema`、`vol_ma`、`rsi`、`highest_high`、`lowest_low` 等任意组合。
  - 规则激活前自动拉取最近 120 根已收盘 K 线做**历史回测预览**，展示触发次数与最近触发时间，回复「确认」后生效。
- **全自然语言群聊交互（零学习成本）**
  - 支持一句话多意图与中英文标的自动映射，例如：
    - `@机器人 把苹果删了，加上微软，再看看英伟达和比特币现在什么形态`
    - `@机器人 帮我加个监控，ETH 15分钟线长下影锤头线且站上MA20就提醒我`
    - `@机器人 把1号规则先关掉`
- **多平台双通道：Telegram + 企业微信（均免公网域名 / 免备案）**
  - **Telegram Bot（零新增依赖）**：直接复用 `httpx` 实现 `getUpdates` 长轮询与 `sendMessage`（Markdown 自动转 Telegram HTML，遇 400 自动纯文本兜底、遇 429 按 `retry_after` 重试）；支持私聊、群内 `@bot`、`/status@bot` 斜杠命令及直接回复 Bot 消息。
  - **企业微信群机器人 Webhook + 智能机器人长连接**：Webhook 负责 7×24 稳定推送（内置 18 条/分钟限流），官方 `wecom-aibot-sdk` 负责群内 `@` 双向交互。
  - 两个平台的 `chat_ids` 与 `allowed_users` 权限白名单完全隔离。
- **严格的已收盘 K 线判定与持久化防重**
  - 每次拉取 `count + 1` 根 K 线，按 `closeTime <= now - 1s` 过滤未收盘 K 线，杜绝盘中假信号。
  - SQLite 持久化监控列表、自定义规则、每条 `(规则, 标的, 周期)` 去重状态与历史；推送失败时不写去重标记，下一轮自动重试。

---

## 系统架构

```text
                       ┌─────────────────────────────┐
                       │    Binance Public Spot API   │  (免 API Key，公开行情)
                       │  /api/v3/klines & exchangeInfo│
                       └──────────────┬──────────────┘
                                      │ 每 45s 并发轮询（过滤未收盘 K 线）
                                      ▼
┌─────────────────────┐      ┌─────────────────────┐      ┌──────────────────────┐
│    Telegram Bot     │◀────▶│  Command & Monitor  │─────▶│ Telegram / 企微群推  │
│ (getUpdates 长轮询) │      │  + Safe AST Engine  │      │  (7×24 稳定告警推送)  │
│  企微智能机器人      │◀────▶│                     │      └──────────────────────┘
│ (WebSocket 长连接)  │      └──────────┬──────────┘
└─────────────────────┘                 │ 仅自然语言交互/设规则时调用
                                        ▼
                             ┌─────────────────────┐      ┌──────────────────────┐
                             │ DeepSeek-Flash LLM  │      │   SQLite 持久化存储   │
                             │ (意图路由 + 规则编译) │      │ (标的/规则/去重/历史) │
                             └─────────────────────┘      └──────────────────────┘
```

---

## 快速开始

### 1. 克隆并安装依赖

要求 Python 3.10+：

```bash
git clone https://github.com/therealemery/binance-kline-alert.git
cd binance-kline-alert

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 2. 配置环境变量与 YAML

```bash
cp config.example.yaml config.yaml
cp .env.example .env
```

编辑 `.env`（可只配 Telegram、只配企微，或两者同时配）：

```ini
# 可选：DeepSeek API Key（启用自然语言交互与自定义形态编译，兼容任意 OpenAI 格式接口）
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash

# Telegram Bot（找 @BotFather 执行 /newbot 获取）
TELEGRAM_BOT_TOKEN=
# 推送目标 chat_id，支持逗号分隔（群聊通常为 -100... 开头）
TELEGRAM_CHAT_IDS=
TELEGRAM_PROXY=

# 企业微信（若仅用 Telegram 可留空）
WECOM_WEBHOOK_URL=
WECOM_BOT_ID=
WECOM_BOT_SECRET=

# 可选：代理（海外 VPS 留空直连即可；国内环境可填 http://127.0.0.1:7890）
BINANCE_PROXY=
```

### 3. 验收与运行

```bash
# 1) 校验监控标的在币安现货是否可交易
binance-alert -c config.yaml validate-symbols

# 2) 单次干跑检查（仅打印，不推送）
binance-alert -c config.yaml check-once

# 3) 测试 DeepSeek 自然语言规则编译 + 币安最近 120 根已收盘 K 线回测
binance-alert -c config.yaml test-rule "NVDA 和 BTC 1小时线放量1.5倍阳包阴"

# 4) 发送测试卡片验证 Telegram / 企微推送
binance-alert -c config.yaml test-telegram
binance-alert -c config.yaml test-webhook

# 5) 正式常驻运行
binance-alert -c config.yaml run
```

---

## 群内交互示例

在 Telegram 私聊、Telegram 群聊（`@bot` / `/status` / 直接回复 Bot 消息）或企业微信群（`@机器人`）：

- **查行情 / 状态**：`/status`、「现在都在监控哪些标的？」「看看英伟达和比特币最新收盘怎么样」「最近触发过哪些信号？」
- **增删标的**：「帮我加上微软和狗狗币」「把苹果删了吧」
- **新增自定义形态**：「帮我加个监控，ETH 15分钟线出现长下影锤头线且站上 MA20 提醒我」→ 查看回测预览后回复「确认」或「好的，开启吧」
- **管理规则 / 推送**：`/rules`、「把1号规则先关掉」「删除2号规则」「先暂停推送」「恢复推送」

也同时支持零 LLM 消耗的快捷命令：`状态`、`列表`、`添加`、`删除`、`检查`、`规则`、`历史`、`暂停`、`恢复`、`帮助`（支持带 `/` 与 `@botname` 后缀）。

---

## 生产部署

建议部署在**新加坡 / 日本 / 香港**等海外轻量服务器（1C1G 即绰绰有余，常驻内存约 50–80 MB，可同时直连币安、Telegram 与企业微信；注意避开受币安 451 限制的美国节点）。

### 方式 1：systemd（轻量推荐）

```bash
sudo mkdir -p /opt/binance-kline-alert/data
# 将项目文件与 config.yaml / .env 放到 /opt/binance-kline-alert
chmod 600 /opt/binance-kline-alert/.env

python3 -m venv /opt/binance-kline-alert/.venv
/opt/binance-kline-alert/.venv/bin/pip install /opt/binance-kline-alert

# 按实际运行用户检查 deploy/binance-kline-alert.service 中的 User/Group
sudo cp deploy/binance-kline-alert.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now binance-kline-alert
journalctl -u binance-kline-alert -f
```

### 方式 2：Docker Compose

```bash
docker compose up -d --build
docker compose logs -f
```

> 提示：Telegram `getUpdates` 长轮询与企业微信智能机器人长连接同一时间均只允许 **1 个**实例运行，请勿用同一组凭证在多台机器同时启动。

---

## 开发与测试

```bash
pytest -v
```

## 免责声明

本项目仅用于公开市场行情监控与技术学习研究，不构成任何投资建议，不包含自动交易或资金操作功能。使用者应自行评估市场风险并遵守所在地区的法律法规与交易所/平台服务条款。

## License

[MIT](./LICENSE)
