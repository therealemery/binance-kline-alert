from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def _replace(match: re.Match[str]) -> str:
            var_name = match.group(1)
            default = match.group(2) if match.group(2) is not None else ""
            env_val = os.getenv(var_name)
            return env_val if (env_val is not None and env_val != "") else default

        return _ENV_PATTERN.sub(_replace, value).strip()
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


@dataclass
class BinanceConfig:
    base_url: str = "https://api.binance.com"
    proxy: str | None = None
    symbols: list[str] = field(default_factory=lambda: ["BTCUSDT", "ETHUSDT", "NVDABUSDT", "TSLABUSDT"])
    interval: str = "1h"
    poll_interval_seconds: int = 45
    max_concurrency: int = 5
    timeout_seconds: float = 15.0


@dataclass
class AiBotConfig:
    enabled: bool = True
    bot_id: str = ""
    secret: str = ""
    push_alerts: bool = False
    allowed_users: list[str] = field(default_factory=list)

    @property
    def is_ready(self) -> bool:
        return self.enabled and bool(self.bot_id.strip()) and bool(self.secret.strip())


@dataclass
class WeComConfig:
    webhook_urls: list[str] = field(default_factory=list)
    aibot: AiBotConfig = field(default_factory=AiBotConfig)


@dataclass
class LLMConfig:
    enabled: bool = True
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    timeout_seconds: float = 30.0
    backtest_candles: int = 120

    @property
    def is_ready(self) -> bool:
        return self.enabled and bool(self.api_key.strip())


@dataclass
class AlertConfig:
    enable_default_engulfing: bool = True
    enable_volume_filter: bool = True
    min_body_ratio: Decimal = Decimal("1.0")
    min_body_pct: Decimal = Decimal("0.001")
    timezone: str = "Asia/Shanghai"


@dataclass
class StorageConfig:
    sqlite_path: str = "./data/alert_state.sqlite3"


@dataclass
class LoggingConfig:
    level: str = "INFO"


@dataclass
class AppConfig:
    binance: BinanceConfig = field(default_factory=BinanceConfig)
    wecom: WeComConfig = field(default_factory=WeComConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    alert: AlertConfig = field(default_factory=AlertConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def _normalize_webhook_urls(raw: Any) -> list[str]:
    if not raw:
        return []
    items: list[str] = []
    if isinstance(raw, str):
        raw_list = [raw]
    elif isinstance(raw, list):
        raw_list = [str(x) for x in raw if x]
    else:
        raw_list = []

    for item in raw_list:
        for part in item.split(","):
            url = part.strip()
            if url:
                items.append(url)
    return list(dict.fromkeys(items))


def load_config(config_path: str | Path = "config.yaml", env_path: str | Path | None = None) -> AppConfig:
    """Load configuration from YAML + .env / environment variables."""
    if env_path is not None:
        load_dotenv(dotenv_path=env_path, override=False)
    else:
        load_dotenv(override=False)

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}. Copy config.example.yaml to {path} first."
        )

    with path.open("r", encoding="utf-8") as fh:
        raw_data = yaml.safe_load(fh) or {}

    data = _expand_env(raw_data)

    b_raw = data.get("binance") or {}
    proxy = b_raw.get("proxy") or os.getenv("BINANCE_PROXY") or None
    if isinstance(proxy, str) and not proxy.strip():
        proxy = None

    symbols = [str(s).strip().upper() for s in (b_raw.get("symbols") or []) if str(s).strip()]
    binance_cfg = BinanceConfig(
        base_url=str(b_raw.get("base_url") or "https://api.binance.com").rstrip("/"),
        proxy=proxy,
        symbols=list(dict.fromkeys(symbols)),
        interval=str(b_raw.get("interval") or "1h"),
        poll_interval_seconds=max(10, int(b_raw.get("poll_interval_seconds", 45))),
        max_concurrency=max(1, int(b_raw.get("max_concurrency", 5))),
        timeout_seconds=float(b_raw.get("timeout_seconds", 15.0)),
    )

    w_raw = data.get("wecom") or {}
    webhook_urls = _normalize_webhook_urls(w_raw.get("webhook_urls"))
    if not webhook_urls and os.getenv("WECOM_WEBHOOK_URL"):
        webhook_urls = _normalize_webhook_urls(os.getenv("WECOM_WEBHOOK_URL"))

    ai_raw = w_raw.get("aibot") or {}
    aibot_cfg = AiBotConfig(
        enabled=bool(ai_raw.get("enabled", True)),
        bot_id=str(ai_raw.get("bot_id") or os.getenv("WECOM_BOT_ID") or "").strip(),
        secret=str(ai_raw.get("secret") or os.getenv("WECOM_BOT_SECRET") or "").strip(),
        push_alerts=bool(ai_raw.get("push_alerts", False)),
        allowed_users=[str(u).strip() for u in (ai_raw.get("allowed_users") or []) if str(u).strip()],
    )
    wecom_cfg = WeComConfig(webhook_urls=webhook_urls, aibot=aibot_cfg)

    llm_raw = data.get("llm") or {}
    llm_cfg = LLMConfig(
        enabled=bool(llm_raw.get("enabled", True)),
        api_key=str(llm_raw.get("api_key") or os.getenv("DEEPSEEK_API_KEY") or "").strip(),
        base_url=str(llm_raw.get("base_url") or os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/"),
        model=str(llm_raw.get("model") or os.getenv("DEEPSEEK_MODEL") or "deepseek-flash").strip(),
        timeout_seconds=float(llm_raw.get("timeout_seconds", 30.0)),
        backtest_candles=max(20, min(500, int(llm_raw.get("backtest_candles", 120)))),
    )

    a_raw = data.get("alert") or {}
    alert_cfg = AlertConfig(
        enable_default_engulfing=bool(a_raw.get("enable_default_engulfing", True)),
        enable_volume_filter=bool(a_raw.get("enable_volume_filter", True)),
        min_body_ratio=Decimal(str(a_raw.get("min_body_ratio", 1.0))),
        min_body_pct=Decimal(str(a_raw.get("min_body_pct", 0.001))),
        timezone=str(a_raw.get("timezone") or "Asia/Shanghai"),
    )

    s_raw = data.get("storage") or {}
    storage_cfg = StorageConfig(
        sqlite_path=str(s_raw.get("sqlite_path") or "./data/alert_state.sqlite3")
    )

    l_raw = data.get("logging") or {}
    logging_cfg = LoggingConfig(level=str(l_raw.get("level") or "INFO").upper())

    return AppConfig(
        binance=binance_cfg,
        wecom=wecom_cfg,
        llm=llm_cfg,
        alert=alert_cfg,
        storage=storage_cfg,
        logging=logging_cfg,
    )
