#!/usr/bin/env python
"""
智策 Web - A 股多智能体分析终端
FastAPI 后端：股票搜索 + SSE 实时流 + 静态文件服务

特性：
- SSE event 携带 agent_id 序号，支持同一角色多个 agent 输出区隔
- 进度信息：当前步骤 / 总步骤
- 决策结果翻译为中文字段
"""
import os
import sys
import json
import asyncio
import queue
import traceback
import threading
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime
from typing import Optional, Any

# ── 模型配置 ────────────────────────────────────────────────
# 预设模型模板(仅展示用,不含 API Key)
PRESET_TEMPLATES = [
    {"id": "ark-code-latest", "name": "Ark Coding Plan (火山方舟)", "provider": "openai", "base_url": "https://ark.cn-beijing.volces.com/api/coding/v3"},
    {"id": "deepseek-chat", "name": "DeepSeek Chat", "provider": "openai", "base_url": "https://api.deepseek.com/v1"},
    {"id": "deepseek-reasoner", "name": "DeepSeek R1 (推理)", "provider": "openai", "base_url": "https://api.deepseek.com/v1"},
    {"id": "gpt-4o", "name": "GPT-4o", "provider": "openai", "base_url": "https://api.openai.com/v1"},
    {"id": "gpt-4o-mini", "name": "GPT-4o mini", "provider": "openai", "base_url": "https://api.openai.com/v1"},
    {"id": "claude-sonnet-4-20250514", "name": "Claude Sonnet 4", "provider": "anthropic", "base_url": ""},
    {"id": "claude-3-5-haiku-20241022", "name": "Claude 3.5 Haiku", "provider": "anthropic", "base_url": ""},
    {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash", "provider": "google_genai", "base_url": ""},
    {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro", "provider": "google_genai", "base_url": ""},
    {"id": "qwen-plus", "name": "通义千问 Plus", "provider": "openai", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    {"id": "qwen-turbo", "name": "通义千问 Turbo", "provider": "openai", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    {"id": "doubao-pro-32k", "name": "豆包 Pro 32k", "provider": "openai", "base_url": "https://ark.cn-beijing.volces.com/api/v3"},
]

# 用户已配置的模型列表(持久化到 models.json)
# 每条: {id, name, provider, model_id, api_key, base_url}
MODELS_FILE = Path(__file__).parent / "models.json"

def load_user_models() -> list[dict]:
    """从 models.json 加载用户配置的模型"""
    if MODELS_FILE.exists():
        try:
            data = json.loads(MODELS_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []
    # 首次初始化:从 .env 创建默认模型
    default_key = os.environ.get("OPENAI_API_KEY", "")
    default_base = os.environ.get("OPENAI_API_BASE", "https://ark.cn-beijing.volces.com/api/coding/v3")
    default_model = {
        "id": "ark-code-latest",
        "name": "Ark Coding Plan (默认)",
        "provider": "openai",
        "model_id": "ark-code-latest",
        "api_key": default_key,
        "base_url": default_base,
    }
    save_user_models([default_model])
    return [default_model]

def save_user_models(models: list[dict]):
    """保存模型配置到 models.json"""
    MODELS_FILE.write_text(json.dumps(models, ensure_ascii=False, indent=2), encoding="utf-8")

def find_user_model(model_id: str) -> Optional[dict]:
    """根据 model_id 查找用户配置的模型"""
    for m in load_user_models():
        if m.get("model_id") == model_id or m.get("id") == model_id:
            return m
    return None

# 角色与模型类型的映射
# quick_thinking: 分析师、研究员、交易员、辩论者
# deep_thinking: 研究经理(辩论裁判)、风险管理(风险裁判)
PHASE_MODEL_TIER = {
    "market_analyst": "quick",
    "social_analyst": "quick",
    "news_analyst": "quick",
    "fundamentals_analyst": "quick",
    "summariser": "quick",
    "bull_bear": "quick",
    "research_manager": "deep",
    "trader": "quick",
    "risk_debate": "quick",
    "risk_judge": "deep",
}

# ── 加载 .env ──────────────────────────────────────────────
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value

# ── akshare 适配层（必须在分析框架导入之前）──────────
import akshare_adapter  # noqa: F401

from fastapi import FastAPI, Request, Body
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── 股票搜索(东方财富搜索 API)──────────────────────────────
def search_stock(keyword: str) -> list[dict]:
    """通过东方财富搜索 API 查询股票代码"""
    url = (
        "https://searchapi.eastmoney.com/api/suggest/get?"
        f"input={urllib.parse.quote(keyword)}&type=14&"
        "token=D43BF722C8E33BDC906FB84D85E326E8&count=10"
    )
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    })
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    results = []
    for item in (data.get("QuotationCodeTable", {}).get("Data") or []):
        if item.get("Classify") == "AStock":
            results.append({
                "code": item["Code"],
                "name": item["Name"],
                "market": item.get("SecurityTypeName", ""),
                "pinyin": item.get("PinYin", ""),
            })
    return results


# ── 分析框架初始化（惰性加载）─────────────────────────
_ta: Optional["TradingAgentsGraph"] = None
_ta_lock = threading.Lock()
_ta_model_config: Optional[dict] = None  # {"deep": ..., "quick": ...}

def _create_custom_llm(model_name: str):
    """用用户配置的 api_key + base_url 创建 LLM,如果没找到则回退到框架默认"""
    user_model = find_user_model(model_name)
    if user_model and user_model.get("api_key"):
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=user_model.get("model_id", model_name),
            openai_api_key=user_model["api_key"],
            openai_api_base=user_model.get("base_url") or "https://api.openai.com/v1",
            temperature=0,
            # 关闭 GLM/ARK 思维链推理,大幅降低延迟(15s/次 -> 2s/次)
            # 基本面分析师有8个工具=9次LLM推理,思维链开启时总耗时>2分钟
            extra_body={"thinking": {"type": "disabled"}},
        )
    # 回退:用框架默认(读环境变量)
    from tradingagents.llm import build_chat_model
    return build_chat_model("openai", model_name)

def get_ta():
    global _ta
    if _ta is None:
        with _ta_lock:
            if _ta is None:
                from tradingagents import TradingAgentsConfig, TradingAgentsGraph
                deep = (_ta_model_config or {}).get("deep", "ark-code-latest")
                quick = (_ta_model_config or {}).get("quick", "ark-code-latest")
                config = TradingAgentsConfig(
                    llm_provider="openai",
                    deep_think_llm=deep,
                    quick_think_llm=quick,
                    max_debate_rounds=1,
                    max_risk_discuss_rounds=1,
                    max_recur_limit=100,
                    response_language="zh-CN",
                )
                _ta = TradingAgentsGraph(config=config)
                # Monkey-patch: 用自定义 LLM 工厂替换框架的 _create_llm
                _ta._create_llm = lambda model: _create_custom_llm(model)
                # 清除 cached_property 缓存,使 deep_thinking_llm / quick_thinking_llm 重新调用 _create_llm
                for prop_name in ["deep_thinking_llm", "quick_thinking_llm"]:
                    _ta.__dict__.pop(prop_name, None)
    return _ta


# ── FastAPI 应用 ────────────────────────────────────────────
app = FastAPI(title="智策 - A股多智能体分析终端")

# 静态文件
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = static_dir / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/api/search")
async def api_search(keyword: str):
    """股票搜索 API"""
    try:
        results = search_stock(keyword)
        return JSONResponse({"ok": True, "data": results})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ── 进度和角色定义 ────────────────────────────────────────────
# 完整流程图(按执行顺序)
PHASE_DEFINITIONS = [
    {"step": 1,  "id": "market_analyst",      "label": "市场分析师",       "agents": ["Market Analyst"],              "emoji": "📈"},
    {"step": 2,  "id": "social_analyst",      "label": "情绪分析师",       "agents": ["Social Sentiment Analyst", "Social Analyst"], "emoji": "📱"},
    {"step": 3,  "id": "news_analyst",        "label": "新闻分析师",       "agents": ["News Analyst"],                 "emoji": "📰"},
    {"step": 4,  "id": "fundamentals_analyst","label": "基本面分析师",     "agents": ["Fundamentals Analyst"],         "emoji": "📊"},
    {"step": 5,  "id": "summariser",          "label": "情景总结",         "agents": ["Situation Summariser"],         "emoji": "📝"},
    {"step": 6,  "id": "bull_bear",           "label": "牛熊辩论",         "agents": ["Bull Researcher", "Bear Researcher"], "emoji": "⚖️"},
    {"step": 7,  "id": "research_manager",    "label": "辩论裁判",         "agents": ["Research Manager"],             "emoji": "👨‍⚖️"},
    {"step": 8,  "id": "trader",              "label": "交易员",           "agents": ["Trader"],                       "emoji": "💰"},
    {"step": 9,  "id": "risk_debate",         "label": "风险评估",         "agents": ["Aggressive Analyst", "Conservative Analyst", "Neutral Analyst"], "emoji": "🛡️"},
    {"step": 10, "id": "risk_judge",          "label": "风险裁判",         "agents": ["Risk Judge"],                   "emoji": "⚡"},
    {"step": 11, "id": "done",                "label": "分析完成",         "agents": [],                               "emoji": "✅"},
]
TOTAL_STEPS = 11

# agent name → phase id 映射
AGENT_TO_PHASE = {}
for p in PHASE_DEFINITIONS:
    for a in p["agents"]:
        AGENT_TO_PHASE[a.lower()] = p["id"]


def detect_phase(msg_name: str, content: str) -> Optional[dict]:
    """根据消息 name 或内容检测阶段

    框架传来的 msg.name 有时为空、大小写不一、或包含变体名。
    这里做多级匹配:精准 -> 模糊 -> 内容关键词 -> 当前阶段 fallback。
    """
    name_lower = (msg_name or "").lower().strip()

    # 1. 精准匹配
    if name_lower in AGENT_TO_PHASE:
        pid = AGENT_TO_PHASE[name_lower]
        for p in PHASE_DEFINITIONS:
            if p["id"] == pid:
                return p

    # 2. 模糊匹配(优先匹配长名称避免误判)
    if name_lower:
        sorted_agents = sorted(
            [(ag, p) for p in PHASE_DEFINITIONS for ag in p["agents"]],
            key=lambda x: -len(x[0])
        )
        for ag, p in sorted_agents:
            if ag.lower() in name_lower or name_lower in ag.lower():
                return p

    # 3. 特殊关键词匹配(处理框架内部的变体名)
    if name_lower:
        if 'sentiment' in name_lower or 'social' in name_lower:
            return PHASE_DEFINITIONS[1]  # social_analyst
        if 'summariser' in name_lower or 'summarizer' in name_lower or 'situation' in name_lower:
            return PHASE_DEFINITIONS[4]  # summariser
        if 'bull' in name_lower:
            return PHASE_DEFINITIONS[5]  # bull_bear
        if 'bear' in name_lower:
            return PHASE_DEFINITIONS[5]  # bull_bear
        if 'research_manager' in name_lower or 'research manager' in name_lower:
            return PHASE_DEFINITIONS[6]  # research_manager
        if 'aggressive' in name_lower:
            return PHASE_DEFINITIONS[8]  # risk_debate
        if 'conservative' in name_lower:
            return PHASE_DEFINITIONS[8]  # risk_debate
        if 'neutral' in name_lower:
            return PHASE_DEFINITIONS[8]  # risk_debate
        if 'risk_judge' in name_lower or 'risk manager' in name_lower or 'riskjudge' in name_lower:
            return PHASE_DEFINITIONS[9]  # risk_judge

    # 4. 从内容前 200 字符判断
    content_lower = (content or "").lower()[:200]
    if any(kw in content_lower for kw in ["市场分析", "技术分析", "market analysis", "技术面"]):
        return PHASE_DEFINITIONS[0]
    if any(kw in content_lower for kw in ["情绪", "social", "sentiment", "舆情"]):
        return PHASE_DEFINITIONS[1]
    if any(kw in content_lower for kw in ["基本面", "fundamental", "财务"]):
        return PHASE_DEFINITIONS[3]
    if any(kw in content_lower for kw in ["新闻", "news", "媒体报道"]):
        return PHASE_DEFINITIONS[2]
    if any(kw in content_lower for kw in ["总结", "summar", "情景"]):
        return PHASE_DEFINITIONS[4]
    if any(kw in content_lower for kw in ["看多", "看空", "bull", "bear"]):
        return PHASE_DEFINITIONS[5]
    if any(kw in content_lower for kw in ["交易", "trade", "仓位"]):
        return PHASE_DEFINITIONS[7]
    if any(kw in content_lower for kw in ["风险", "risk", "激进", "保守"]):
        return PHASE_DEFINITIONS[8]

    return None


def get_agent_idx_in_phase(msg_name: str, phase_def: dict) -> int:
    """返回该 agent 在所属阶段中的序号(从0开始)"""
    name_lower = (msg_name or "").lower().strip()
    for i, ag in enumerate(phase_def["agents"]):
        if ag.lower() in name_lower or name_lower in ag.lower():
            return i
    return 0


def _extract_state(state) -> dict:
    """从 AgentState 提取关键信息"""
    try:
        raw = {}
        if hasattr(state, "model_dump"):
            raw = state.model_dump()
        elif isinstance(state, dict):
            raw = state

        data = {}
        fields_trunc = 12000  # 增大截断长度,确保报告完整
        if raw.get("market_report"):
            data["market_report"] = raw["market_report"][:fields_trunc]
        if raw.get("fundamentals_report"):
            data["fundamentals_report"] = raw["fundamentals_report"][:fields_trunc]
        if raw.get("news_report"):
            data["news_report"] = raw["news_report"][:fields_trunc]
        if raw.get("sentiment_report"):
            data["sentiment_report"] = raw["sentiment_report"][:fields_trunc]
        if raw.get("situation_summary") or raw.get("summariser_report"):
            data["summariser_report"] = (raw.get("situation_summary") or raw.get("summariser_report"))[:fields_trunc]

        debate = raw.get("investment_debate_state")
        if debate:
            data["investment_debate"] = {
                "bull_history": (debate.get("bull_history") or "")[:fields_trunc],
                "bear_history": (debate.get("bear_history") or "")[:fields_trunc],
                "judge_decision": (debate.get("judge_decision") or "")[:fields_trunc],
                "count": debate.get("count", 0),
            }

        risk_debate = raw.get("risk_debate_state")
        if risk_debate:
            data["risk_debate"] = {
                "aggressive_history": (risk_debate.get("aggressive_history") or "")[:fields_trunc],
                "conservative_history": (risk_debate.get("conservative_history") or "")[:fields_trunc],
                "neutral_history": (risk_debate.get("neutral_history") or "")[:fields_trunc],
                "judge_decision": (risk_debate.get("judge_decision") or "")[:fields_trunc],
                "count": risk_debate.get("count", 0),
            }

        if raw.get("trader_investment_plan"):
            data["trader_plan"] = raw["trader_investment_plan"][:fields_trunc]
        if raw.get("final_trade_decision"):
            data["final_decision"] = raw["final_trade_decision"][:fields_trunc]

        rec = raw.get("final_trade_recommendation")
        if rec and isinstance(rec, dict):
            data["recommendation"] = _translate_recommendation(rec)
        elif rec and hasattr(rec, "model_dump"):
            data["recommendation"] = _translate_recommendation(rec.model_dump())

        return data
    except Exception:
        return {}


SIGNAL_MAP = {"BUY": "买入", "SELL": "卖出", "HOLD": "持有"}

# 决策维度翻译(rationale 中的常见英文关键词→中文)
RATIONALE_TRANSLATIONS = {
    # 技术分析
    "Technical": "技术面", "trend": "趋势", "momentum": "动量", "volume": "成交量",
    "support": "支撑", "resistance": "阻力", "breakout": "突破", "pullback": "回调",
    "moving average": "均线", "MACD": "MACD", "RSI": "RSI", "Bollinger": "布林带",
    "overbought": "超买", "oversold": "超卖",
    # 基本面
    "Fundamental": "基本面", "P/E": "市盈率", "EPS": "每股收益", "revenue": "营收",
    "profit": "利润", "margin": "利润率", "growth": "增长", "valuation": "估值",
    "debt": "负债", "cash flow": "现金流", "dividend": "股息",
    # 市场情绪
    "Sentiment": "情绪", "Sentiment": "市场情绪", "Fear": "恐慌", "Greed": "贪婪",
    "retail": "散户", "institutional": "机构", "flow": "资金流向",
    # 新闻/事件
    "News": "新闻", "policy": "政策", "regulatory": "监管", "earnings": "财报",
    "guidance": "指引", "forecast": "预测", "risk": "风险",
    # 通用
    "bullish": "看涨", "bearish": "看跌", "neutral": "中性",
    "strong": "强", "weak": "弱", "positive": "积极", "negative": "消极",
    "high": "高", "low": "低", "increase": "增加", "decrease": "减少",
    "price": "价格", "target": "目标", "stop-loss": "止损", "entry": "入场",
    "exit": "出场", "position": "仓位", "allocation": "配置",
    "DCF": "DCF估值", "discounted": "折现", "free cash": "自由现金流",
    "sector": "板块", "industry": "行业", "peer": "同业", "comparison": "对比",
}

def translate_rationale(text: str) -> str:
    """将决策依据中的英文关键词批量替换为中文"""
    if not text:
        return text
    result = text
    # 优先替换长词组
    sorted_keys = sorted(RATIONALE_TRANSLATIONS.keys(), key=len, reverse=True)
    for key in sorted_keys:
        cn = RATIONALE_TRANSLATIONS[key]
        # 不区分大小写替换
        import re
        result = re.sub(re.escape(key), cn, result, flags=re.IGNORECASE)
    return result

def _translate_recommendation(rec: dict) -> dict:
    """将英文 recommendation 字段翻译为中文"""
    r = dict(rec)
    if "signal" in r and r["signal"] in SIGNAL_MAP:
        r["signal_cn"] = SIGNAL_MAP[r["signal"]]
    else:
        r["signal_cn"] = r.get("signal", "-")
    # 翻译决策依据
    if "rationale" in r and r["rationale"]:
        r["rationale_cn"] = translate_rationale(r["rationale"])
    else:
        r["rationale_cn"] = r.get("rationale_cn", r.get("rationale", "-"))
    return r


# ── 模型管理 API ─────────────────────────────────────────────

class ModelConfig(BaseModel):
    name: str
    provider: str = "openai"
    model_id: str
    api_key: str
    base_url: str = ""

class ModelTestRequest(BaseModel):
    model_id: str
    api_key: str = ""
    base_url: str = ""
    provider: str = "openai"

@app.get("/api/models")
async def api_models():
    """返回用户已配置的模型列表 + 预设模板 + 角色-模型层级映射"""
    user_models = load_user_models()
    safe_models = []
    for m in user_models:
        safe = dict(m)
        key = safe.get("api_key", "")
        safe["api_key_masked"] = key[:8] + "****" + key[-4:] if len(key) > 12 else "****"
        safe.pop("api_key", None)
        safe_models.append(safe)
    return JSONResponse({
        "ok": True,
        "models": safe_models,
        "templates": PRESET_TEMPLATES,
        "phase_tiers": PHASE_MODEL_TIER,
        "current": {
            "deep_think_llm": _ta_model_config.get("deep", "ark-code-latest") if _ta_model_config else "ark-code-latest",
            "quick_think_llm": _ta_model_config.get("quick", "ark-code-latest") if _ta_model_config else "ark-code-latest",
            "llm_provider": "openai",
        },
    })

@app.post("/api/models")
async def api_add_model(cfg: ModelConfig):
    """添加或更新一个模型配置"""
    models = load_user_models()
    existing = None
    for m in models:
        if m.get("model_id") == cfg.model_id:
            existing = m
            break
    if existing:
        existing["name"] = cfg.name
        existing["provider"] = cfg.provider
        existing["api_key"] = cfg.api_key
        existing["base_url"] = cfg.base_url
    else:
        models.append({
            "id": cfg.model_id,
            "name": cfg.name,
            "provider": cfg.provider,
            "model_id": cfg.model_id,
            "api_key": cfg.api_key,
            "base_url": cfg.base_url,
        })
    save_user_models(models)
    return JSONResponse({"ok": True, "message": f"模型 {cfg.name} 已保存"})

@app.delete("/api/models/{model_id}")
async def api_delete_model(model_id: str):
    """删除一个模型配置"""
    models = load_user_models()
    before = len(models)
    models = [m for m in models if m.get("model_id") != model_id]
    if len(models) == before:
        return JSONResponse({"ok": False, "error": "模型不存在"}, status_code=404)
    save_user_models(models)
    return JSONResponse({"ok": True, "message": f"模型 {model_id} 已删除"})

@app.post("/api/models/test")
async def api_test_model(req: ModelTestRequest):
    """测试模型连通性"""
    try:
        api_key = req.api_key
        base_url = req.base_url
        if not api_key:
            user_model = find_user_model(req.model_id)
            if user_model:
                api_key = user_model.get("api_key", "")
                base_url = base_url or user_model.get("base_url", "")
        if not api_key:
            return JSONResponse({"ok": False, "error": "缺少 API Key"}, status_code=400)
        if not base_url:
            base_url = "https://api.openai.com/v1"
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(
            model=req.model_id,
            openai_api_key=api_key,
            openai_api_base=base_url,
            temperature=0,
            timeout=15,
        )
        resp = llm.invoke("请回复:连接成功")
        content = resp.content if hasattr(resp, "content") else str(resp)
        return JSONResponse({
            "ok": True,
            "message": "模型连接成功",
            "response": content[:200],
            "model": req.model_id,
        })
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.get("/api/analyze")
async def api_analyze(ticker: str, date: str = "", model_overrides: str = ""):
    """启动多智能体分析，通过 SSE 实时推送状态"""
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

    # 解析 model_overrides(JSON 字符串)
    overrides = {}
    if model_overrides:
        try:
            overrides = json.loads(model_overrides)
        except json.JSONDecodeError:
            pass

    # 构建 TA 配置:如果用户覆盖了模型,则重新创建 TA 实例
    # 模型覆盖分两层:
    #   1. deep_think / quick_think 全局替换
    #   2. 单角色覆盖(需要框架支持,当前仅支持两层全局替换)
    deep_model = overrides.get("deep_think") or overrides.get("default") or "ark-code-latest"
    quick_model = overrides.get("quick_think") or overrides.get("default") or "ark-code-latest"

    # 如果有模型覆盖,重建 TA 实例
    global _ta, _ta_model_config
    if overrides.get("default") or overrides.get("deep_think") or overrides.get("quick_think"):
        _ta = None  # 强制重建
        _ta_model_config = {
            "deep": deep_model,
            "quick": quick_model,
        }
    else:
        _ta_model_config = None

    async def event_stream():
        loop = asyncio.get_event_loop()

        def sse(event_type: str, data: dict) -> str:
            return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        yield sse("start", {
            "ticker": ticker,
            "date": date,
            "phases": PHASE_DEFINITIONS,
            "total_steps": TOTAL_STEPS,
            "timestamp": datetime.now().isoformat(),
        })

        _msg_q = queue.Queue()
        cancelled = threading.Event()

        current_step = 0          # 当前执行到的步骤序号(PhaseDefinitions 中的 step)
        current_phase_obj = None  # 当前阶段对象
        phases_completed = set()
        agent_seq: dict[str, int] = {}   # name.lower() → 累计出现次数

        def on_message(msg):
            if cancelled.is_set():
                return
            try:
                msg_type = getattr(msg, "type", "unknown")
                content = getattr(msg, "content", "")
                name = getattr(msg, "name", "") or ""
                name_lower = name.lower()

                nonlocal current_step, current_phase_obj

                # 检测当前属于哪个阶段
                phase_obj = detect_phase(name, content)
                if phase_obj is None:
                    # fallback:如果检测不到阶段,用当前阶段
                    if current_phase_obj:
                        phase_obj = current_phase_obj
                    else:
                        phase_obj = PHASE_DEFINITIONS[0]

                # 更新步进:严格按顺序推进,不跳步
                if phase_obj["step"] > current_step:
                    # 前一个阶段标记完成
                    if current_step > 0:
                        for p in PHASE_DEFINITIONS:
                            if p["step"] == current_step:
                                phases_completed.add(p["id"])
                                break
                    current_step = phase_obj["step"]

                current_phase_obj = phase_obj

                # 计算此 agent 在同角色中的序号(第几次出现)
                agent_seq[name_lower] = agent_seq.get(name_lower, 0) + 1
                seq_in_phase = get_agent_idx_in_phase(name, phase_obj)

                # 构造 SSE 消息
                _msg_q.put(("message", {
                    "phase": {
                        "id": phase_obj["id"],
                        "label": phase_obj["label"],
                        "step": phase_obj["step"],
                        "total_steps": TOTAL_STEPS,
                        "emoji": phase_obj["emoji"],
                    },
                    "phases_completed": sorted(phases_completed),
                    "current_step": current_step,
                    "msg_type": msg_type,
                    "name": name,
                    "agent_seq": agent_seq[name_lower],
                    "seq_in_phase": seq_in_phase,
                    "content": (content or "")[:12000],
                    "timestamp": datetime.now().isoformat(),
                }))
            except Exception:
                pass

        def on_state(state):
            if cancelled.is_set():
                return
            try:
                state_data = _extract_state(state)
                if state_data:
                    phase = current_phase_obj
                    _msg_q.put(("state", {
                        "state_data": state_data,
                        "phase": {
                            "id": phase["id"],
                            "label": phase["label"],
                            "step": phase["step"],
                            "total_steps": TOTAL_STEPS,
                            "emoji": phase["emoji"],
                        } if phase else None,
                    }))
            except Exception:
                pass

        def run_analysis():
            try:
                ta = get_ta()
                # ContextVar 不跨线程，子线程需重新 set_config，否则 load_prompt 的
                # response_language fallback 到 en-US，导致 agent 输出变成英文
                from tradingagents import set_config
                set_config(ta.config)
                final_state, recommendation = ta.propagate(
                    ticker, date,
                    on_message=on_message,
                    on_state=on_state,
                )
                rec_data = _translate_recommendation(
                    recommendation.model_dump() if recommendation else {}
                )
                # 标记所有阶段完成
                all_phases = sorted(set(p["id"] for p in PHASE_DEFINITIONS if p["id"] != "done"))
                _msg_q.put(("done", {
                    "decision": rec_data,
                    "final_state": _extract_state(final_state),
                    "all_phases_completed": all_phases,
                }))
            except Exception as e:
                _msg_q.put(("error", {
                    "error": str(e),
                    "traceback": traceback.format_exc(),
                }))
            finally:
                _msg_q.put(None)

        t = threading.Thread(target=run_analysis, daemon=True)
        t.start()

        try:
            while True:
                try:
                    item = _msg_q.get(timeout=0.3)
                except queue.Empty:
                    continue
                if item is None:
                    break
                event_type, data = item
                yield sse(event_type, data)
        except asyncio.CancelledError:
            cancelled.set()
            yield sse("cancel", {"message": "用户取消"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── 入口 ────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8460,
        log_level="info",
    )
