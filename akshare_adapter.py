"""
akshare 适配层 - A 股数据接口

使用 akshare 替换框架内置的数据流，支持 A 股全市场行情、
财务报表、新闻资讯等数据获取。通过 monkey-patch 在 import 时自动生效。

支持范围：
  - A 股行情（沪深京）: stock_zh_a_hist
  - A 股财务报表: stock_financial_report_sina
  - 指数行情: stock_zh_index_daily / index_us_stock_sina
  - 新闻: 东方财富搜索API (50条个股新闻) + 财新网 + 央视新闻联播

不支持的功能（返回 [NO_DATA]）：
  - 分析师评级（原框架专有，已用东方财富研报替代）
  - 机构持仓 / 内部人交易 / 做空兴趣（原框架专有）
  - 美股个股行情/财报

用法：import akshare_adapter（必须在分析框架导入之前），自动执行 monkey-patch。

Ticker 映射规则：
  - 6 位数字代码（如 600519、000001、300750）→ 直接用于 akshare
  - 带 .SH / .SZ / .BJ 后缀 → 去除后缀取 6 位数字
  - 美股代码（如 AAPL）→ 尝试 akshare 美股接口，可能不可用
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

import pandas as pd

logger = logging.getLogger(__name__)

_NO_DATA_PREFIX = "[NO_DATA]"

# ─── Ticker 解析 ───────────────────────────────────────────────

def _normalize_a_share_code(ticker: str) -> str | None:
    """将各种 A 股代码格式统一为 6 位数字。返回 None 表示非 A 股。"""
    t = ticker.strip().upper()
    # 去除常见后缀
    for suffix in (".SH", ".SZ", ".BJ", ".SS", ".TWO", ".TW"):
        if t.endswith(suffix):
            t = t[:-len(suffix)]
            break
    # 纯数字 6 位 → A 股
    if t.isdigit() and len(t) == 6:
        return t
    # 带前缀的 (sh600519, sz000001)
    for prefix in ("sh", "sz", "bj"):
        if t.startswith(prefix) and t[2:].isdigit() and len(t[2:]) == 6:
            return t[2:]
    return None


def _is_a_share(ticker: str) -> bool:
    return _normalize_a_share_code(ticker) is not None


# ─── 行情数据 ──────────────────────────────────────────────────

def _get_a_share_history(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """通过 akshare 获取 A 股日 K 线（前复权）。使用新浪数据源（东方财富接口不可达）。"""
    import akshare as ak
    # akshare 的日期格式是 YYYYMMDD
    sd = start_date.replace("-", "")
    ed = end_date.replace("-", "")

    # 确定交易所前缀：6开头=sh, 0/3开头=sz, 8/4开头=bj
    if code.startswith("6"):
        sina_symbol = f"sh{code}"
    elif code.startswith("0") or code.startswith("3") or code.startswith("2"):
        sina_symbol = f"sz{code}"
    elif code.startswith("8") or code.startswith("4"):
        sina_symbol = f"bj{code}"
    else:
        sina_symbol = f"sh{code}"  # 默认

    try:
        df = ak.stock_zh_a_daily(
            symbol=sina_symbol,
            start_date=sd,
            end_date=ed,
            adjust="qfq",
        )
    except Exception as exc:
        raise RuntimeError(f"akshare 获取 {code} 行情失败: {exc}") from exc

    if df is None or df.empty:
        return pd.DataFrame()

    # akshare sina 返回列: date open high low close volume amount outstanding_share turnover
    out = pd.DataFrame()
    out["Date"] = pd.to_datetime(df["date"])
    out["Open"] = df["open"]
    out["High"] = df["high"]
    out["Low"] = df["low"]
    out["Close"] = df["close"]
    out["Adj Close"] = df["close"]  # qfq 已经是复权价
    out["Volume"] = df["volume"]
    return out


def _resolve_history_with_cache_ak(symbol: str, curr_date_dt: datetime) -> tuple[str, pd.DataFrame, list[str]]:
    """akshare 版本的 _resolve_history_with_cache — 获取 15 年日 K 线并缓存。"""
    # 获取缓存目录（兼容未初始化 config 的情况）
    try:
        from tradingagents.config import get_config
        config = get_config()
        cache_dir = Path(str(config.data_cache_dir))
    except Exception:
        # config 未初始化时使用默认目录
        cache_dir = Path("results/data_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)

    code = _normalize_a_share_code(symbol)
    candidates = [symbol] if code is None else [code, symbol]

    download_end_dt = curr_date_dt + timedelta(days=1)
    start_dt = (pd.Timestamp(curr_date_dt) - pd.DateOffset(years=15)).to_pydatetime()
    start_date_str = pd.Timestamp(start_dt).strftime("%Y-%m-%d")
    end_date_str = pd.Timestamp(download_end_dt).strftime("%Y-%m-%d")

    data = pd.DataFrame()
    resolved_symbol = candidates[0]
    last_error: Exception | None = None

    for candidate in candidates:
        code = _normalize_a_share_code(candidate)
        if code is None:
            continue
        data_file = cache_dir / f"{candidate}-YFin-data.csv"
        # 尝试读缓存
        if data_file.exists():
            try:
                cached = pd.read_csv(data_file)
                cached["Date"] = pd.to_datetime(cached["Date"])
                # 检查缓存覆盖范围
                if not cached.empty:
                    if cached["Date"].min() <= pd.Timestamp(start_dt) and \
                       cached["Date"].max() >= pd.Timestamp(curr_date_dt):
                        # 历史日期直接用缓存
                        if curr_date_dt.date() < datetime.now().date():
                            return candidate, cached, candidates
                        # 今天的数据检查新鲜度
                        age = datetime.now() - datetime.fromtimestamp(data_file.stat().st_mtime)
                        if age < timedelta(hours=12):
                            return candidate, cached, candidates
            except Exception:
                pass

        # 下载
        try:
            candidate_data = _get_a_share_history(code, start_date_str, end_date_str)
        except Exception as exc:
            last_error = exc
            continue

        if not candidate_data.empty:
            candidate_data.to_csv(data_file, index=False)
            data = candidate_data
            resolved_symbol = candidate
            break

    if data.empty:
        tried = ", ".join(candidates)
        if last_error is not None:
            raise RuntimeError(
                f"Failed to fetch market data for symbol '{symbol}' via akshare (tried: {tried})"
            ) from last_error
        raise ValueError(f"No market data found for symbol '{symbol}' via akshare (tried: {tried}).")

    return resolved_symbol, data, candidates


# ─── 替换函数 ──────────────────────────────────────────────────

def get_yfin_data_online_ak(
    symbol: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """获取 OHLCV 股票数据（通过 akshare），返回 CSV 字符串。"""
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    if start_dt > end_dt:
        return f"[TOOL_ERROR] start_date must be on or before end_date"

    try:
        resolved_symbol, data, candidates = _resolve_history_with_cache_ak(symbol, end_dt)
    except (ValueError, RuntimeError) as exc:
        return f"[TOOL_ERROR] {exc}"

    data = data.copy()
    data["Date"] = pd.to_datetime(data["Date"])
    if data["Date"].dt.tz is not None:
        data["Date"] = data["Date"].dt.tz_localize(None)
    mask = (data["Date"] >= pd.Timestamp(start_dt)) & (data["Date"] <= pd.Timestamp(end_dt))
    sliced = data.loc[mask].copy()

    if sliced.empty:
        return (
            f"{_NO_DATA_PREFIX} No data found for symbol '{symbol}' "
            f"between {start_date} and {end_date}"
        )

    numeric_columns = ["Open", "High", "Low", "Close", "Adj Close"]
    for col in numeric_columns:
        if col in sliced.columns:
            sliced[col] = sliced[col].round(2)

    sliced["Date"] = sliced["Date"].dt.strftime("%Y-%m-%d")
    csv_string = sliced.to_csv(index=False)

    header = f"# Stock data for {resolved_symbol} from {start_date} to {end_date}\n"
    header += f"# Total records: {len(sliced)}\n"
    header += "# Note: OHLC values are前复权 (qfq) adjusted.\n"
    header += f"# Data source: akshare (A股)\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"

    return header + csv_string


def get_stock_stats_indicators_batch_ak(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicators: Annotated[list[str], "list of technical indicators"],
    curr_date: Annotated[str, "current trading date in YYYY-MM-DD format"],
    look_back_days: Annotated[int, "look-back window in days"] = 30,
) -> str:
    """计算技术指标（通过 akshare 行情数据 + stockstats）。"""
    from stockstats import wrap
    from dateutil.relativedelta import relativedelta

    curr_date_dt = datetime.strptime(curr_date, "%Y-%m-%d")

    try:
        resolved_symbol, data, _ = _resolve_history_with_cache_ak(symbol, curr_date_dt)
    except (ValueError, RuntimeError) as exc:
        return f"[TOOL_ERROR] {exc}"

    data = data.copy()
    data["Date"] = pd.to_datetime(data["Date"])
    if data["Date"].dt.tz is not None:
        data["Date"] = data["Date"].dt.tz_localize(None)
    data = data.loc[data["Date"] <= pd.Timestamp(curr_date_dt)].copy()
    if data.empty:
        return f"{_NO_DATA_PREFIX} No market data found for symbol '{symbol}' on or before {curr_date}."

    df = wrap(data)
    if df["Date"].dt.tz is not None:
        df["Date"] = df["Date"].dt.tz_localize(None)
    df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")

    before = curr_date_dt - relativedelta(days=look_back_days)
    before_str = before.strftime("%Y-%m-%d")
    end_str = curr_date_dt.strftime("%Y-%m-%d")

    # 复用原始 BEST_IND_PARAMS 描述
    try:
        from tradingagents.dataflows.yfinance import BEST_IND_PARAMS
    except ImportError:
        BEST_IND_PARAMS = {}

    sections: list[str] = []
    for ind in indicators:
        try:
            df[ind]  # trigger stockstats
        except Exception:
            sections.append(f"## {ind}: [ERROR] Indicator not computable\n")
            continue
        formatted = df[ind].apply(lambda v: "N/A" if pd.isna(v) else str(v))
        ind_data = dict(zip(df["Date"], formatted, strict=False))
        sorted_dates = sorted(d for d in ind_data if before_str <= d <= end_str)
        if sorted_dates:
            ind_string = "".join(f"{d}: {ind_data[d]}\n" for d in sorted_dates)
        else:
            ind_string = "(no trading days in window)\n"
        desc = BEST_IND_PARAMS.get(ind, "")
        sections.append(
            f"## {ind} values from {before_str} to {end_str} (chronological, trading days only):\n\n"
            + ind_string + "\n\n" + desc
        )
    return "\n\n".join(sections)


def get_stock_stats_indicators_window_ak(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    curr_date: Annotated[str, "The current trading date you are trading on, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"],
) -> str:
    """单个技术指标包装。"""
    return get_stock_stats_indicators_batch_ak(symbol, [indicator], curr_date, look_back_days)


# ─── 财务报表 ──────────────────────────────────────────────────

def _get_financial_report_sina(code: str, report_type: str) -> pd.DataFrame:
    """通过 akshare 获取新浪财务报表。report_type: 资产负债表/利润表/现金流量表"""
    import akshare as ak
    try:
        df = ak.stock_financial_report_sina(stock=code, symbol=report_type)
        return df if df is not None else pd.DataFrame()
    except Exception as exc:
        logger.debug("akshare financial report failed for %s: %s", code, exc)
        return pd.DataFrame()


def get_balance_sheet_ak(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str | None, "current trading date in YYYY-MM-DD format"] = None,
) -> str:
    """获取资产负债表（通过 akshare/新浪财经）。"""
    code = _normalize_a_share_code(ticker)
    if code is None:
        return f"{_NO_DATA_PREFIX} Balance sheet via akshare only supports A-share tickers, got '{ticker}'"

    df = _get_financial_report_sina(code, "资产负债表")
    if df.empty:
        return f"{_NO_DATA_PREFIX} No balance sheet data found for '{ticker}' via akshare"

    # 按日期过滤
    if curr_date and "报告日" in df.columns:
        cutoff = pd.Timestamp(curr_date)
        df["报告日"] = pd.to_datetime(df["报告日"], format="%Y%m%d", errors="coerce")
        df = df[df["报告日"] <= cutoff].copy()

    # 季度/年度过滤
    if freq == "annual" and "报告日" in df.columns:
        # 只保留年报（12月31日）
        df = df[df["报告日"].dt.month == 12].copy()

    if df.empty:
        return f"{_NO_DATA_PREFIX} No balance sheet data for '{ticker}' ({freq}) as of {curr_date or 'latest'}"

    csv_string = df.to_csv(index=False)
    header = f"# Balance Sheet data for {ticker} ({freq})\n"
    header += "# Reported currency: CNY\n"
    header += f"# Data source: akshare/新浪财经\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + csv_string


def get_income_statement_ak(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str | None, "current trading date in YYYY-MM-DD format"] = None,
) -> str:
    """获取利润表（通过 akshare/新浪财经）。"""
    code = _normalize_a_share_code(ticker)
    if code is None:
        return f"{_NO_DATA_PREFIX} Income statement via akshare only supports A-share tickers, got '{ticker}'"

    df = _get_financial_report_sina(code, "利润表")
    if df.empty:
        return f"{_NO_DATA_PREFIX} No income statement data found for '{ticker}' via akshare"

    if curr_date and "报告日" in df.columns:
        cutoff = pd.Timestamp(curr_date)
        df["报告日"] = pd.to_datetime(df["报告日"], format="%Y%m%d", errors="coerce")
        df = df[df["报告日"] <= cutoff].copy()

    if freq == "annual" and "报告日" in df.columns:
        df = df[df["报告日"].dt.month == 12].copy()

    if df.empty:
        return f"{_NO_DATA_PREFIX} No income statement data for '{ticker}' ({freq}) as of {curr_date or 'latest'}"

    csv_string = df.to_csv(index=False)
    header = f"# Income Statement data for {ticker} ({freq})\n"
    header += "# Reported currency: CNY\n"
    header += f"# Data source: akshare/新浪财经\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + csv_string


def get_cashflow_ak(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str | None, "current trading date in YYYY-MM-DD format"] = None,
) -> str:
    """获取现金流量表（通过 akshare/新浪财经）。"""
    code = _normalize_a_share_code(ticker)
    if code is None:
        return f"{_NO_DATA_PREFIX} Cash flow via akshare only supports A-share tickers, got '{ticker}'"

    df = _get_financial_report_sina(code, "现金流量表")
    if df.empty:
        return f"{_NO_DATA_PREFIX} No cash flow data found for '{ticker}' via akshare"

    if curr_date and "报告日" in df.columns:
        cutoff = pd.Timestamp(curr_date)
        df["报告日"] = pd.to_datetime(df["报告日"], format="%Y%m%d", errors="coerce")
        df = df[df["报告日"] <= cutoff].copy()

    if freq == "annual" and "报告日" in df.columns:
        df = df[df["报告日"].dt.month == 12].copy()

    if df.empty:
        return f"{_NO_DATA_PREFIX} No cash flow data for '{ticker}' ({freq}) as of {curr_date or 'latest'}"

    csv_string = df.to_csv(index=False)
    header = f"# Cash Flow data for {ticker} ({freq})\n"
    header += "# Reported currency: CNY\n"
    header += f"# Data source: akshare/新浪财经\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + csv_string


# ─── 基本面 ────────────────────────────────────────────────────

def get_fundamentals_ak(
    ticker: Annotated[str, "ticker symbol of the company"],
    curr_date: Annotated[str | None, "current trading date in YYYY-MM-DD format"] = None,
) -> str:
    """获取基本面概览（通过 akshare）。"""
    code = _normalize_a_share_code(ticker)
    if code is None:
        return f"{_NO_DATA_PREFIX} Fundamentals via akshare only supports A-share tickers, got '{ticker}'"

    import akshare as ak
    lines: list[str] = []

    # 个股信息（使用新浪接口，东方财富不可达）
    try:
        # 尝试通过新浪获取个股信息
        sina_symbol = f"sh{code}" if code.startswith("6") else f"sz{code}"
        df_info = ak.stock_zh_a_daily(symbol=sina_symbol, start_date="20250101", end_date="20250102", adjust="")
        lines.append(f"Name: {ticker}")
        lines.append(f"Symbol: {code}")
    except Exception:
        lines.append(f"Name: {ticker}")
        lines.append(f"Symbol: {code}")

    # 尝试获取行业信息（通过东方财富，可能不可达）
    try:
        info = ak.stock_individual_info_em(symbol=code)
        info_dict = dict(zip(info["item"], info["value"]))
        if "行业" in info_dict:
            lines.append(f"Industry: {info_dict['行业']}")
        if "总市值" in info_dict:
            lines.append(f"Market Cap: {info_dict['总市值']}")
        if "流通市值" in info_dict:
            lines.append(f"Float Market Cap: {info_dict['流通市值']}")
    except Exception:
        pass

    # 从利润表获取最新财务数据
    try:
        income_df = _get_financial_report_sina(code, "利润表")
        if not income_df.empty and "报告日" in income_df.columns:
            income_df["报告日"] = pd.to_datetime(income_df["报告日"], format="%Y%m%d", errors="coerce")
            if curr_date:
                cutoff = pd.Timestamp(curr_date)
                income_df = income_df[income_df["报告日"] <= cutoff]
            if not income_df.empty:
                latest = income_df.iloc[0]
                if "营业总收入" in income_df.columns and pd.notna(latest.get("营业总收入")):
                    lines.append(f"Revenue (latest): {latest['营业总收入']:,.0f}")
                if "净利润" in income_df.columns and pd.notna(latest.get("净利润")):
                    lines.append(f"Net Income (latest): {latest['净利润']:,.0f}")
    except Exception:
        pass

    header = f"# Company Fundamentals for {ticker}\n"
    if curr_date:
        header += f"# Current trading date: {curr_date}\n"
    header += "# Reported currency: CNY\n"
    header += f"# Data source: akshare\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + "\n".join(lines)


# ─── 市场环境 ──────────────────────────────────────────────────

def get_market_context_ak(
    ticker: Annotated[str, "ticker symbol; used to resolve the local index region"],
    curr_date: Annotated[str, "current trading date in YYYY-MM-DD format"],
    look_back_days: Annotated[int, "look-back window in days"] = 5,
) -> str:
    """返回市场宏观环境快照（通过 akshare）。"""
    import akshare as ak

    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    start_dt = curr_dt - timedelta(days=look_back_days + 5)

    sections = [f"# Market context for {ticker} as of {curr_date} (window={look_back_days}d)"]

    code = _normalize_a_share_code(ticker)

    # 本地指数
    if code:
        if code.startswith("6") and not code.startswith("68"):
            idx_symbol = "sh000001"  # 上证综指
            idx_label = "Shanghai Composite"
        elif code.startswith("0") or code.startswith("3") or code.startswith("2"):
            idx_symbol = "sz399001"  # 深证成指
            idx_label = "Shenzhen Component"
        elif code.startswith("8") or code.startswith("4"):
            idx_symbol = "bj899050"  # 北证50
            idx_label = "BSE 50"
        else:
            idx_symbol = "sh000001"
            idx_label = "Shanghai Composite"
    else:
        idx_symbol = "sh000001"
        idx_label = "Shanghai Composite"

    try:
        idx_df = ak.stock_zh_index_daily(symbol=idx_symbol)
        if idx_df is not None and not idx_df.empty:
            idx_df["date"] = pd.to_datetime(idx_df["date"])
            window = idx_df[(idx_df["date"] >= pd.Timestamp(start_dt)) & (idx_df["date"] <= pd.Timestamp(curr_dt))]
            if not window.empty:
                last_close = float(window["close"].iloc[-1])
                first_close = float(window["close"].iloc[0])
                pct = (last_close / first_close - 1.0) * 100.0 if first_close else 0.0
                high = float(window["close"].max())
                low = float(window["close"].min())
                sections.append(
                    f"## Local index: {idx_label} ({idx_symbol})\n"
                    f"Latest close: {last_close:.2f}\n"
                    f"Window change: {pct:+.2f}%\n"
                    f"Window range: low {low:.2f} -- high {high:.2f}"
                )
    except Exception as exc:
        sections.append(f"## Local index: {idx_label} ({idx_symbol})\n[TOOL_ERROR] {exc!s}")

    header = f"# Data source: akshare\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    return header + "\n\n".join(sections)


# ─── 分析师评级（东方财富研报）────────────────────────────────

def get_analyst_ratings_ak(
    ticker: str,
    curr_date: str | None = None,
) -> str:
    """用东方财富研报数据替代 yfinance 分析师评级。

    返回最近 30 条研报评级，包含：评级、机构、研究员、日期、
    盈利预测（EPS/PE）。
    """
    try:
        import akshare as ak
        df = ak.stock_research_report_em(symbol=ticker)
        if df is None or df.empty:
            return f"{_NO_DATA_PREFIX} No analyst ratings found for '{ticker}' via akshare."

        # 筛选日期（curr_date 之前的报告）
        if curr_date:
            try:
                df["日期"] = df["日期"].astype(str)
                df = df[df["日期"] <= curr_date]
            except Exception:
                pass

        if df.empty:
            return f"{_NO_DATA_PREFIX} No analyst ratings for '{ticker}' on or before {curr_date}."

        # 取最近 30 条
        df = df.head(30)

        # 统计评级分布
        rating_col = "东财评级" if "东财评级" in df.columns else None
        rating_dist = {}
        if rating_col:
            rating_dist = df[rating_col].value_counts().to_dict()

        lines = []
        lines.append(f"# Analyst Ratings for {ticker} (东方财富研报)")
        lines.append(f"# Data source: akshare -> 东方财富")
        if curr_date:
            lines.append(f"# Filtered to reports on or before {curr_date}")
        lines.append(f"# Total recent reports: {len(df)}")
        lines.append("")

        # 评级分布
        if rating_dist:
            lines.append("## Rating Distribution (评级分布)")
            for rating, count in sorted(rating_dist.items(), key=lambda x: -x[1]):
                lines.append(f"  {rating}: {count}")
            lines.append("")

        # 详细报告列表
        lines.append("## Recent Research Reports (最近研报)")
        for _, row in df.iterrows():
            report_name = str(row.get("报告名称", "")).strip()
            rating = str(row.get("东财评级", "")).strip()
            org = str(row.get("机构", "")).strip()
            date = str(row.get("日期", "")).strip()
            eps_1 = str(row.get("2026-盈利预测-收益", "")).strip()
            pe_1 = str(row.get("2026-盈利预测-市盈率", "")).strip()
            eps_2 = str(row.get("2027-盈利预测-收益", "")).strip()
            pe_2 = str(row.get("2027-盈利预测-市盈率", "")).strip()
            industry = str(row.get("行业", "")).strip()

            line = f"- [{date}] {rating} | {org}"
            if industry:
                line += f" | 行业: {industry}"
            line += f"\n  报告: {report_name}"
            if eps_1 and eps_1 != "nan":
                line += f"\n  盈利预测: 2026 EPS={eps_1} (PE={pe_1})"
            if eps_2 and eps_2 != "nan":
                line += f", 2027 EPS={eps_2} (PE={pe_2})"
            lines.append(line)

        lines.append("")
        lines.append("## Note")
        lines.append("- 评级说明: 买入 > 增持 > 中性 > 减持 > 卖出")
        lines.append("- 数据来源: 东方财富研报中心")
        lines.append("- EPS/PE 为机构盈利预测值，非实际财报数据")

        return "\n".join(lines)
    except Exception as e:
        logger.error("get_analyst_ratings_ak error: %s", e)
        return f"{_NO_DATA_PREFIX} Failed to fetch analyst ratings for '{ticker}' via akshare: {e}"


# ─── 不支持的功能 ──────────────────────────────────────────────

def _not_supported(func_name: str):
    def _impl(*args, **kwargs):
        ticker = args[0] if args else kwargs.get("ticker", "?")
        return (
            f"{_NO_DATA_PREFIX} {func_name} for '{ticker}': "
            f"akshare adapter does not support this data source. "
            f"Only A-share market data, financial statements, and fundamentals are available."
        )
    _impl.__name__ = func_name
    return _impl


# ─── 新闻数据（东方财富搜索API + 财新网 + 央视新闻联播）─────────

def _fetch_eastmoney_news(code: str, page_size: int = 50) -> list[dict]:
    """通过东方财富搜索 API 获取个股新闻（比 akshare.stock_news_em 的 10 条更多）。

    直接调用 eastmoney search-api-web 接口，返回 JSON 列表。
    """
    import json
    import re
    import urllib.request

    param_json = json.dumps({
        "uid": "",
        "keyword": code,
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientType": "web",
        "clientVersion": "curr",
        "param": {
            "cmsArticleWebOld": {
                "searchScope": "default",
                "sort": "default",
                "pageIndex": 1,
                "pageSize": page_size,
                "preTag": "",
                "postTag": ""
            }
        }
    })
    url = (
        "https://search-api-web.eastmoney.com/search/jsonp?cb=jQuery&param="
        + urllib.parse.quote(param_json)
    )
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read().decode("utf-8")
    m = re.search(r'jQuery\((.*)\)', raw)
    if not m:
        return []
    data = json.loads(m.group(1))
    articles = data.get("result", {}).get("cmsArticleWebOld", [])
    return articles


def fetch_news_ak(ticker: str, start_date: str, end_date: str) -> str:
    """用东方财富搜索 API + akshare stock_news_em 双源获取个股新闻。

    优先使用东方财富搜索 API（可获取 50 条），回退到 akshare.stock_news_em（10 条）。
    按日期范围过滤，超出范围的新闻不返回。
    """
    from datetime import datetime as _dt

    code = _normalize_a_share_code(ticker)
    if code is None:
        return (
            f"{_NO_DATA_PREFIX} News search only supports A-share tickers; "
            f"got '{ticker}'."
        )

    # 解析日期范围
    try:
        start_dt = _dt.strptime(start_date, "%Y-%m-%d")
        end_dt = _dt.strptime(end_date, "%Y-%m-%d")
    except Exception:
        start_dt = end_dt = None

    articles = []

    # 数据源 1: 东方财富搜索 API（50 条）
    try:
        raw_articles = _fetch_eastmoney_news(code, page_size=50)
        for a in raw_articles:
            pub_str = a.get("date", "")
            try:
                pub_dt = _dt.strptime(pub_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pub_dt = None
            articles.append({
                "title": a.get("title", "(无标题)"),
                "content": a.get("content", ""),
                "source": a.get("mediaName", "未知"),
                "date": pub_str,
                "dt": pub_dt,
                "link": a.get("url", ""),
            })
    except Exception as exc:
        logger.debug("eastmoney search API failed for %s: %s", code, exc)

    # 数据源 2: akshare stock_news_em 回退
    if not articles:
        try:
            import akshare as ak
            df = ak.stock_news_em(symbol=code)
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    pub_str = str(row.get("发布时间", ""))
                    try:
                        pub_dt = _dt.strptime(pub_str, "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        pub_dt = None
                    articles.append({
                        "title": str(row.get("新闻标题", "(无标题)")),
                        "content": str(row.get("新闻内容", "")),
                        "source": str(row.get("文章来源", "未知")),
                        "date": pub_str,
                        "dt": pub_dt,
                        "link": str(row.get("新闻链接", "")),
                    })
        except Exception as exc:
            logger.debug("stock_news_em fallback failed for %s: %s", code, exc)

    if not articles:
        return f"{_NO_DATA_PREFIX} No news found for {ticker} from any source."

    # 日期过滤：只保留 start_date ~ end_date 范围内的新闻
    # 如果无法解析日期，则保留（宁多勿缺）
    filtered = []
    for a in articles:
        if a["dt"] is None:
            filtered.append(a)
        elif start_dt and end_dt and start_dt <= a["dt"] <= end_dt:
            filtered.append(a)
        elif not start_dt or not end_dt:
            filtered.append(a)

    # 如果日期过滤后为空，使用全部新闻（带提示）
    if not filtered:
        filtered = articles
        date_note = (
            f" (注意: 未找到 {start_date} ~ {end_date} 范围内的新闻，"
            f"以下为最近可用新闻)"
        )
    else:
        date_note = f" (filtered: {start_date} ~ {end_date})"

    # 按日期降序排列
    filtered.sort(key=lambda x: x["dt"] or _dt.min, reverse=True)

    news_str = (
        f"## {ticker} News (东方财富 + akshare){date_note}, "
        f"{len(filtered)} articles:\n\n"
    )
    for a in filtered:
        news_str += f"### {a['title']} (source: {a['source']})\n"
        news_str += f"Published: {a['date']}\n"
        if a["content"]:
            news_str += f"{a['content'][:500]}\n"
        if a["link"]:
            news_str += f"Link: {a['link']}\n"
        news_str += "\n"

    return news_str


def get_global_news_ak(curr_date: str, look_back_days: int = 7, limit: int = 10) -> str:
    """用财新网 + 央视新闻联播双源替代 yfinance 全球新闻。

    数据源 1: akshare.stock_news_main_cx（财新网，100 条最新财经新闻）
    数据源 2: akshare.news_cctv（央视新闻联播文字版，逐日获取）
    """
    from datetime import datetime as _dt, timedelta as _td

    try:
        curr_dt = _dt.strptime(curr_date, "%Y-%m-%d")
        start_dt = curr_dt - _td(days=look_back_days)
    except Exception as exc:
        logger.debug("get_global_news_ak date parse failed", exc_info=True)
        return f"[TOOL_ERROR] Failed to parse date for global news: {exc!s}"

    all_news = []

    # 数据源 1: 财新网最新财经新闻
    try:
        import akshare as ak
        df = ak.stock_news_main_cx()
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                all_news.append({
                    "date": "",
                    "title": str(row.get("summary", "(无标题)")),
                    "content": "",
                    "source": f"财新网-{row.get('tag', '')}",
                    "link": str(row.get("url", "")),
                    "sort_key": 0,  # 财新新闻不知道日期，排前面
                })
    except Exception as exc:
        logger.debug("stock_news_main_cx failed: %s", exc)

    # 数据源 2: 央视新闻联播（按日期逐日获取）
    try:
        import akshare as ak
        date_to_check = start_dt
        while date_to_check <= curr_dt and len(all_news) < limit * 5:
            date_str = date_to_check.strftime("%Y%m%d")
            try:
                df = ak.news_cctv(date=date_str)
                if df is not None and not df.empty:
                    for _, row in df.iterrows():
                        all_news.append({
                            "date": str(row.get("date", date_str)),
                            "title": str(row.get("title", "")),
                            "content": str(row.get("content", "")),
                            "source": "央视新闻联播",
                            "link": "",
                            "sort_key": 1,
                        })
            except Exception as exc:
                logger.debug("news_cctv failed for %s: %s", date_str, exc)
            date_to_check += _td(days=1)
    except Exception as exc:
        logger.debug("news_cctv global failed: %s", exc)

    if not all_news:
        return (
            f"{_NO_DATA_PREFIX} No global news found between "
            f"{start_dt.strftime('%Y-%m-%d')} and {curr_date}."
        )

    # 去重（按标题）
    seen = set()
    deduped = []
    for n in all_news:
        if n["title"] not in seen:
            seen.add(n["title"])
            deduped.append(n)

    news_str = (
        f"## Global Market News (财新网 + 央视新闻联播), "
        f"from {start_dt.strftime('%Y-%m-%d')} to {curr_date}:\n\n"
    )
    for item in deduped[:limit]:
        news_str += f"### {item['title']} (source: {item['source']})\n"
        if item["date"]:
            news_str += f"Published: {item['date']}\n"
        if item["content"]:
            news_str += f"{item['content'][:400]}\n"
        if item["link"]:
            news_str += f"Link: {item['link']}\n"
        news_str += "\n"

    return news_str


# ─── Monkey-patch ──────────────────────────────────────────────

def _patch_module_bindings(mod, name_map):
    """Patch module-level attribute bindings + StructuredTool.func if present.

    `name_map` is {local_attr_name: new_function}. This handles both
    the common ``from x.y import foo as _foo`` pattern and the
    ``@tool`` decorator (which closes over the function at decoration
    time, so we also need to patch ``StructuredTool.func``).

    The original StructuredTool object is *preserved* (we only patch its
    .func attribute), so any ``from mod import tool_obj`` in other modules
    (e.g. tool_registry.py) still references the same object and thus
    automatically sees the patched behavior.
    """
    from langchain_core.tools import StructuredTool

    for local_name, new_func in name_map.items():
        # Save the original object BEFORE setting module attribute
        orig_obj = getattr(mod, local_name, None)
        # Patch the module attribute
        setattr(mod, local_name, new_func)
        # If the original was a StructuredTool, patch its .func (in-place)
        if isinstance(orig_obj, StructuredTool):
            object.__setattr__(orig_obj, 'func', new_func)


def apply_patch():
    """全面替换分析框架的数据层函数为 akshare 实现。

    Patch 策略（三层）：
    1. yfinance 模块层：替换框架内 `tradingagents.dataflows.yfinance` 模块属性
    2. 工具模块层：通过 _patch_module_bindings() 替换已绑定的名字和 StructuredTool.func
    3. dataflows.news 层：替换 fetch_news / get_global_news_yfinance 等
    """
    import tradingagents.dataflows.yfinance as yf_mod

    yf_mod.get_yfin_data_online = get_yfin_data_online_ak
    yf_mod.get_stock_stats_indicators_batch = get_stock_stats_indicators_batch_ak
    yf_mod.get_stock_stats_indicators_window = get_stock_stats_indicators_window_ak
    yf_mod.get_fundamentals = get_fundamentals_ak
    yf_mod.get_balance_sheet = get_balance_sheet_ak
    yf_mod.get_income_statement = get_income_statement_ak
    yf_mod.get_cashflow = get_cashflow_ak
    yf_mod.get_market_context = get_market_context_ak

    # 不支持的功能 — 返回明确的 no-data 消息（不报错）
    yf_mod.get_analyst_ratings = get_analyst_ratings_ak
    yf_mod.get_earnings_calendar = _not_supported("get_earnings_calendar")
    yf_mod.get_institutional_holders = _not_supported("get_institutional_holders")
    yf_mod.get_insider_transactions = _not_supported("get_insider_transactions")
    yf_mod.get_short_interest = _not_supported("get_short_interest")
    yf_mod.get_dividends_splits = _not_supported("get_dividends_splits")

    yf_mod._resolve_history_with_cache = _resolve_history_with_cache_ak

    # ── 第二层：patch 工具模块中已绑定的名字 + StructuredTool.func ──
    try:
        import tradingagents.agents.utils.fundamental_data_tools as fd_tools
        # 第一轮：patch _get_xxx 别名（plain function 绑定）
        _patch_module_bindings(fd_tools, {
            "_get_fundamentals": yf_mod.get_fundamentals,
            "_get_balance_sheet": yf_mod.get_balance_sheet,
            "_get_cashflow": yf_mod.get_cashflow,
            "_get_income_statement": yf_mod.get_income_statement,
            "_get_analyst_ratings": yf_mod.get_analyst_ratings,
            "_get_institutional_holders": yf_mod.get_institutional_holders,
            "_get_short_interest": yf_mod.get_short_interest,
            "_get_dividends_splits": yf_mod.get_dividends_splits,
        })
        # 第二轮：patch @tool 装饰的 StructuredTool 的 .func（不改模块属性，只修原对象）
        from langchain_core.tools import StructuredTool
        _tool_func_map = {
            "get_fundamentals": fd_tools._get_fundamentals,
            "get_balance_sheet": fd_tools._get_balance_sheet,
            "get_cashflow": fd_tools._get_cashflow,
            "get_income_statement": fd_tools._get_income_statement,
            "get_analyst_ratings": fd_tools._get_analyst_ratings,
            "get_institutional_holders": fd_tools._get_institutional_holders,
            "get_short_interest": fd_tools._get_short_interest,
            "get_dividends_splits": fd_tools._get_dividends_splits,
        }
        for local_name, new_func in _tool_func_map.items():
            obj = getattr(fd_tools, local_name, None)
            if isinstance(obj, StructuredTool):
                object.__setattr__(obj, 'func', new_func)
        logger.info("  → fundamental_data_tools patched")
    except Exception as e:
        logger.warning("  → fundamental_data_tools patch skipped: %s", e)

    try:
        import tradingagents.agents.utils.news_data_tools as nd_tools
        import tradingagents.dataflows.news as news_mod

        # Patch dataflows.news functions with akshare implementations
        news_mod.fetch_news = fetch_news_ak
        news_mod.get_global_news_yfinance = get_global_news_ak
        news_mod.get_news_yfinance = fetch_news_ak  # alias

        _patch_module_bindings(nd_tools, {
            "_get_market_context": yf_mod.get_market_context,
            "_get_earnings_calendar": yf_mod.get_earnings_calendar,
            "_get_insider_transactions": yf_mod.get_insider_transactions,
        })
        # Patch @tool decorated StructuredTool.func in news_data_tools
        from langchain_core.tools import StructuredTool
        _news_tool_func_map = {
            "get_market_context": nd_tools._get_market_context,
            "get_earnings_calendar": nd_tools._get_earnings_calendar,
            "get_insider_transactions": nd_tools._get_insider_transactions,
        }
        for local_name, new_func in _news_tool_func_map.items():
            obj = getattr(nd_tools, local_name, None)
            if isinstance(obj, StructuredTool):
                object.__setattr__(obj, 'func', new_func)
        # get_news and get_global_news: fetch_news / get_global_news_yfinance
        # were imported as-is (not aliased), so the module-level patch
        # of news_mod above is sufficient since the @tool closure resolves
        # the name at call time (it's a global ref).
        # But to be safe, also patch the StructuredTool.func:
        for name in ("get_news", "get_global_news"):
            obj = getattr(nd_tools, name, None)
            if isinstance(obj, StructuredTool):
                func_name = "fetch_news" if name == "get_news" else "get_global_news_yfinance"
                object.__setattr__(obj, 'func', getattr(news_mod, func_name))
        logger.info("  → news_data_tools + dataflows.news patched")
    except Exception as e:
        logger.warning("  → news layer patch skipped: %s", e)

    try:
        import tradingagents.agents.utils.core_stock_tools as cs_tools
        _patch_module_bindings(cs_tools, {
            "get_yfin_data_online": yf_mod.get_yfin_data_online,
        })
        from langchain_core.tools import StructuredTool
        obj = getattr(cs_tools, "get_stock_data", None)
        if isinstance(obj, StructuredTool):
            object.__setattr__(obj, 'func', cs_tools.get_yfin_data_online)
        logger.info("  → core_stock_tools patched")
    except Exception as e:
        logger.warning("  → core_stock_tools patch skipped: %s", e)

    try:
        import tradingagents.agents.utils.technical_indicators_tools as ti_tools
        _patch_module_bindings(ti_tools, {
            "get_stock_stats_indicators_batch": yf_mod.get_stock_stats_indicators_batch,
        })
        # 注意：不要替换 get_indicators 的 func！
        # get_indicators 的签名是 indicator (单数 str)，内部会转换成 indicators 列表
        # 再调用 get_stock_stats_indicators_batch(symbol, indicators, ...)
        # _patch_module_bindings 已经把 ti_tools 模块里的 get_stock_stats_indicators_batch
        # 引用替换成了 akshare 版本，所以 get_indicators 内部调用会正确走到 akshare 实现。
        # 之前把 get_indicators.func 直接替换成 get_stock_stats_indicators_batch 导致
        # 参数名不匹配 (indicator vs indicators)，LLM 传入 indicator=xxx 就报错了。
        logger.info("  → technical_indicators_tools patched")
    except Exception as e:
        logger.warning("  → technical_indicators_tools patch skipped: %s", e)

    logger.info("akshare adapter fully applied")


# 自动应用 patch（import 本模块即生效）
apply_patch()
