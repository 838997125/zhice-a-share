#!/usr/bin/env python
"""
智策 - A股多智能体分析 CLI 入口
- 数据源: akshare（A 股全市场行情/财报/新闻）
- LLM: 通过 .env 或 Web UI 模型配置面板指定
"""
import os
import sys
from pathlib import Path

# 加载 .env
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value

# ── akshare 适配层（必须在分析框架导入之前）────────────
import akshare_adapter  # noqa: F401  (import 即自动 monkey-patch)

from tradingagents import TradingAgentsConfig, TradingAgentsGraph

# 配置
_deep = os.environ.get("DEEP_THINK_LLM", "ark-code-latest")
_quick = os.environ.get("QUICK_THINK_LLM", "ark-code-latest")

config = TradingAgentsConfig(
    llm_provider="openai",
    deep_think_llm=_deep,
    quick_think_llm=_quick,
    max_debate_rounds=1,
    max_risk_discuss_rounds=1,
    max_recur_limit=100,
    response_language="zh-CN",
)

ta = TradingAgentsGraph(config=config)

# 运行分析
ticker = sys.argv[1] if len(sys.argv) > 1 else "600519"
date = sys.argv[2] if len(sys.argv) > 2 else "2025-06-16"

print(f"\n{'='*60}")
print(f"  智策 · A股多智能体分析")
print(f"  股票: {ticker}")
print(f"  日期: {date}")
print(f"  深度模型: {_deep}")
print(f"  快速模型: {_quick}")
print(f"  数据源: akshare (A 股)")
print(f"{'='*60}\n")

result = ta.propagate(ticker, date)

print(f"\n{'='*60}")
print(f"  分析完成")
print(f"{'='*60}\n")

if isinstance(result, dict):
    for key, value in result.items():
        print(f"\n--- {key} ---")
        print(value)
else:
    print(result)
