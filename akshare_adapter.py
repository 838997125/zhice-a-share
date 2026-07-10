"""
akshare 适配层 - A 股数据接口

使用 akshare 替换框架内置的数据流,支持 A 股全市场行情、
财务报表、新闻资讯等数据获取。通过 monkey-patch 在 import 时自动生效。

支持范围：
  - A 股行情（沪深京）: stock_zh_a_hist
  - A 股财务报表: stock_financial_report_sina
  - 指数行情: stock_zh_index_daily / index_us_stock_sina
  - 新闻: 东方财富搜索API (50条个股新闻) + 财新网 + 央视新闻联播
  - 分红送股: stock_dividend_cninfo (巨潮信息网)
  - 财报披露日历: stock_yysj_em (东方财富)
  - 机构/十大股东持仓: stock_main_stock_holder + stock_fund_stock_holder
  - 高管增减持: stock_share_hold_change_sse/szse
  - 融资融券: stock_margin_detail_sse/szse
  - 分析师评级: 东方财富研报

不支持的功能（返回 [NO_DATA]）：
  - 美股个股行情/财报

用法:import akshare_adapter(必须在分析框架导入之前),自动执行 monkey-patch。

Ticker 映射规则:
  - 6 位数字代码(如 600519、000001、300750)→ 直接用于 akshare
  - 带 .SH / .SZ / .BJ 后缀 → 去除后缀取 6 位数字
  - 美股代码(如 AAPL)→ 尝试 akshare 美股接口,可能不可用
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
    """通过 akshare 获取 A 股日 K 线(前复权)。使用新浪数据源(东方财富接口不可达)。"""
    import akshare as ak
    # akshare 的日期格式是 YYYYMMDD
    sd = start_date.replace("-", "")
    ed = end_date.replace("-", "")

    # 确定交易所前缀:6开头=sh, 0/3开头=sz, 8/4开头=bj
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
    """akshare 版本的 _resolve_history_with_cache - 获取 15 年日 K 线并缓存。"""
    # 获取缓存目录(兼容未初始化 config 的情况)
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
                f"通过 akshare 获取股票 '{symbol}' 行情数据失败(已尝试:{tried})"
            ) from last_error
        raise ValueError(f"未通过 akshare 找到股票 '{symbol}' 的行情数据(已尝试:{tried})。")

    return resolved_symbol, data, candidates


# ─── 替换函数 ──────────────────────────────────────────────────

def get_yfin_data_online_ak(
    symbol: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """获取 OHLCV 股票数据(通过 akshare),返回 CSV 字符串。"""
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    if start_dt > end_dt:
        return f"[TOOL_ERROR] 开始日期必须早于或等于结束日期"

    try:
        resolved_symbol, data, candidates = _resolve_history_with_cache_ak(symbol, end_dt)
    except (ValueError, RuntimeError) as exc:
        return f"[TOOL_ERROR] {exc}"  # 保留原始异常信息

    data = data.copy()
    data["Date"] = pd.to_datetime(data["Date"])
    if data["Date"].dt.tz is not None:
        data["Date"] = data["Date"].dt.tz_localize(None)
    mask = (data["Date"] >= pd.Timestamp(start_dt)) & (data["Date"] <= pd.Timestamp(end_dt))
    sliced = data.loc[mask].copy()

    if sliced.empty:
        return (
            f"{_NO_DATA_PREFIX} 未找到股票 '{symbol}' 的数据 "
            f"between {start_date} and {end_date}"
        )

    numeric_columns = ["Open", "High", "Low", "Close", "Adj Close"]
    for col in numeric_columns:
        if col in sliced.columns:
            sliced[col] = sliced[col].round(2)

    sliced["Date"] = sliced["Date"].dt.strftime("%Y-%m-%d")
    csv_string = sliced.to_csv(index=False)

    header = f"# 股票数据:{resolved_symbol},{start_date} 至 {end_date}\n"
    header += f"# 记录总数:{len(sliced)}\n"
    header += "# Note: OHLC values are前复权 (qfq) adjusted.\n"
    header += f"# 数据源:akshare(A股)\n"
    header += f"# 数据获取时间:{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"

    return header + csv_string


def get_stock_stats_indicators_batch_ak(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicators: Annotated[list[str], "list of technical indicators"],
    curr_date: Annotated[str, "current trading date in YYYY-MM-DD format"],
    look_back_days: Annotated[int, "look-back window in days"] = 30,
) -> str:
    """计算技术指标(通过 akshare 行情数据 + stockstats)。"""
    from stockstats import wrap
    from dateutil.relativedelta import relativedelta

    curr_date_dt = datetime.strptime(curr_date, "%Y-%m-%d")

    try:
        resolved_symbol, data, _ = _resolve_history_with_cache_ak(symbol, curr_date_dt)
    except (ValueError, RuntimeError) as exc:
        return f"[TOOL_ERROR] {exc}"  # 保留原始异常信息

    data = data.copy()
    data["Date"] = pd.to_datetime(data["Date"])
    if data["Date"].dt.tz is not None:
        data["Date"] = data["Date"].dt.tz_localize(None)
    data = data.loc[data["Date"] <= pd.Timestamp(curr_date_dt)].copy()
    if data.empty:
        return f"{_NO_DATA_PREFIX} 未找到股票 '{symbol}' 在 {curr_date} 及之前的市场数据。"

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
            sections.append(f"## {ind}:[错误] 指标无法计算\n")
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
            f"## {ind} 指标值,{before_str} 至 {end_str}(按交易日时间顺序):\n\n"
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
    """获取资产负债表(通过 akshare/新浪财经)。"""
    code = _normalize_a_share_code(ticker)
    if code is None:
        return f"{_NO_DATA_PREFIX} 资产负债表仅支持A股代码,当前为 '{ticker}'"

    df = _get_financial_report_sina(code, "资产负债表")
    if df.empty:
        return f"{_NO_DATA_PREFIX} 未通过 akshare 找到 '{ticker}' 的资产负债表数据"

    # 按日期过滤
    if curr_date and "报告日" in df.columns:
        cutoff = pd.Timestamp(curr_date)
        df["报告日"] = pd.to_datetime(df["报告日"], format="%Y%m%d", errors="coerce")
        df = df[df["报告日"] <= cutoff].copy()

    # 季度/年度过滤
    if freq == "annual" and "报告日" in df.columns:
        # 只保留年报(12月31日)
        df = df[df["报告日"].dt.month == 12].copy()

    if df.empty:
        return f"{_NO_DATA_PREFIX} '{ticker}'({freq})截至 {curr_date or '最新'} 无资产负债表数据"

    csv_string = df.to_csv(index=False)
    header = f"# 资产负债表:{ticker}({freq})\n"
    header += "# Reported currency: CNY\n"
    header += f"# 数据源:akshare/新浪财经\n"
    header += f"# 数据获取时间:{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + csv_string


def get_income_statement_ak(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str | None, "current trading date in YYYY-MM-DD format"] = None,
) -> str:
    """获取利润表(通过 akshare/新浪财经)。"""
    code = _normalize_a_share_code(ticker)
    if code is None:
        return f"{_NO_DATA_PREFIX} 利润表仅支持A股代码,当前为 '{ticker}'"

    df = _get_financial_report_sina(code, "利润表")
    if df.empty:
        return f"{_NO_DATA_PREFIX} 未通过 akshare 找到 '{ticker}' 的利润表数据"

    if curr_date and "报告日" in df.columns:
        cutoff = pd.Timestamp(curr_date)
        df["报告日"] = pd.to_datetime(df["报告日"], format="%Y%m%d", errors="coerce")
        df = df[df["报告日"] <= cutoff].copy()

    if freq == "annual" and "报告日" in df.columns:
        df = df[df["报告日"].dt.month == 12].copy()

    if df.empty:
        return f"{_NO_DATA_PREFIX} '{ticker}'({freq})截至 {curr_date or '最新'} 无利润表数据"

    csv_string = df.to_csv(index=False)
    header = f"# 利润表:{ticker}({freq})\n"
    header += "# Reported currency: CNY\n"
    header += f"# 数据源:akshare/新浪财经\n"
    header += f"# 数据获取时间:{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + csv_string


def get_cashflow_ak(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str | None, "current trading date in YYYY-MM-DD format"] = None,
) -> str:
    """获取现金流量表(通过 akshare/新浪财经)。"""
    code = _normalize_a_share_code(ticker)
    if code is None:
        return f"{_NO_DATA_PREFIX} 现金流量表仅支持A股代码,当前为 '{ticker}'"

    df = _get_financial_report_sina(code, "现金流量表")
    if df.empty:
        return f"{_NO_DATA_PREFIX} 未通过 akshare 找到 '{ticker}' 的现金流量表数据"

    if curr_date and "报告日" in df.columns:
        cutoff = pd.Timestamp(curr_date)
        df["报告日"] = pd.to_datetime(df["报告日"], format="%Y%m%d", errors="coerce")
        df = df[df["报告日"] <= cutoff].copy()

    if freq == "annual" and "报告日" in df.columns:
        df = df[df["报告日"].dt.month == 12].copy()

    if df.empty:
        return f"{_NO_DATA_PREFIX} '{ticker}'({freq})截至 {curr_date or '最新'} 无现金流量表数据"

    csv_string = df.to_csv(index=False)
    header = f"# 现金流量表:{ticker}({freq})\n"
    header += "# Reported currency: CNY\n"
    header += f"# 数据源:akshare/新浪财经\n"
    header += f"# 数据获取时间:{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + csv_string


# ─── 基本面 ────────────────────────────────────────────────────

def get_fundamentals_ak(
    ticker: Annotated[str, "ticker symbol of the company"],
    curr_date: Annotated[str | None, "current trading date in YYYY-MM-DD format"] = None,
) -> str:
    """获取基本面概览(通过 akshare)。"""
    code = _normalize_a_share_code(ticker)
    if code is None:
        return f"{_NO_DATA_PREFIX} 基本面数据仅支持A股代码,当前为 '{ticker}'"

    import akshare as ak
    lines: list[str] = []

    # 个股信息(使用新浪接口,东方财富不可达)
    try:
        # 尝试通过新浪获取个股信息
        sina_symbol = f"sh{code}" if code.startswith("6") else f"sz{code}"
        df_info = ak.stock_zh_a_daily(symbol=sina_symbol, start_date="20250101", end_date="20250102", adjust="")
        lines.append(f"Name: {ticker}")
        lines.append(f"Symbol: {code}")
    except Exception:
        lines.append(f"Name: {ticker}")
        lines.append(f"Symbol: {code}")

    # 尝试获取行业信息(通过东方财富,可能不可达)
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

    header = f"# 公司基本面:{ticker}\n"
    if curr_date:
        header += f"# 当前交易日:{curr_date}\n"
    header += "# Reported currency: CNY\n"
    header += f"# 数据源:akshare\n"
    header += f"# 数据获取时间:{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + "\n".join(lines)


# ─── 市场环境 ──────────────────────────────────────────────────

def get_market_context_ak(
    ticker: Annotated[str, "ticker symbol; used to resolve the local index region"],
    curr_date: Annotated[str, "current trading date in YYYY-MM-DD format"],
    look_back_days: Annotated[int, "look-back window in days"] = 5,
) -> str:
    """返回市场宏观环境快照(通过 akshare)。"""
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
                    f"## 本地指数:{idx_label}({idx_symbol})\n"
                    f"Latest close: {last_close:.2f}\n"
                    f"区间涨跌幅:{pct:+.2f}%\n"
                    f"区间范围:最低 {low:.2f} -- 最高 {high:.2f}"
                )
    except Exception as exc:
        sections.append(f"## 本地指数:{idx_label}({idx_symbol})\n[TOOL_ERROR] {exc!s}")

    header = f"# 数据源:akshare\n"
    header += f"# 数据获取时间:{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    return header + "\n\n".join(sections)


# ─── 分析师评级(东方财富研报)────────────────────────────────

def get_analyst_ratings_ak(
    ticker: str,
    curr_date: str | None = None,
) -> str:
    """用东方财富研报数据替代 yfinance 分析师评级。

    返回最近 30 条研报评级,包含:评级、机构、研究员、日期、
    盈利预测(EPS/PE)。
    """
    try:
        import akshare as ak
        df = ak.stock_research_report_em(symbol=ticker)
        if df is None or df.empty:
            return f"{_NO_DATA_PREFIX} 未通过 akshare 找到 '{ticker}' 的分析师评级数据。"

        # 筛选日期(curr_date 之前的报告)
        if curr_date:
            try:
                df["日期"] = df["日期"].astype(str)
                df = df[df["日期"] <= curr_date]
            except Exception:
                pass

        if df.empty:
            return f"{_NO_DATA_PREFIX} '{ticker}' 在 {curr_date} 及之前无分析师评级数据。"

        # 取最近 30 条
        df = df.head(30)

        # 统计评级分布
        rating_col = "东财评级" if "东财评级" in df.columns else None
        rating_dist = {}
        if rating_col:
            rating_dist = df[rating_col].value_counts().to_dict()

        lines = []
        lines.append(f"# Analyst Ratings for {ticker} (东方财富研报)")
        lines.append(f"# 数据源:akshare -> 东方财富")
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
        lines.append("- EPS/PE 为机构盈利预测值,非实际财报数据")

        return "\n".join(lines)
    except Exception as e:
        logger.error("get_analyst_ratings_ak error: %s", e)
        return f"{_NO_DATA_PREFIX} 获取 '{ticker}' 分析师评级数据失败:{e}"


# ─── 分红送股 (stock_dividend_cninfo) ──────────────────────────────

def get_dividends_splits_ak(ticker: str, start_date: str, end_date: str) -> str:
    """用巨潮信息网分红送股数据替代 yfinance dividends/splits。

    返回指定日期范围内的分红送股记录,包含送股比例、转增比例、
    派息比例、股权登记日、除权日等信息。
    """
    try:
        import akshare as ak

        code = _normalize_a_share_code(ticker)
        if code is None:
            return f"{_NO_DATA_PREFIX} 分红送股数据仅支持A股代码;got '{ticker}'."

        df = ak.stock_dividend_cninfo(symbol=code)
        if df is None or df.empty:
            return f"{_NO_DATA_PREFIX} 未找到 '{ticker}' 的分红送股记录。"

        # 日期过滤
        date_col = "实施方案公告日期" if "实施方案公告日期" in df.columns else df.columns[0]
        try:
            df[date_col] = df[date_col].astype(str)
            if start_date:
                df = df[df[date_col] >= start_date]
            if end_date:
                df = df[df[date_col] <= end_date]
        except Exception:
            pass

        if df.empty:
            return f"{_NO_DATA_PREFIX} '{ticker}' 在 {start_date}~{end_date} 范围内无分红送股记录。"

        lines = [
            f"# Dividends & Splits for {ticker} (巨潮信息网)",
            f"# 数据源:akshare -> cninfo",
            f"# 记录数: {len(df)}",
            "",
            "| 公告日期 | 分红类型 | 送股 | 转增 | 派息 | 股权登记日 | 除权日 | 派息日 | 说明 |",
            "|----------|----------|------|------|------|-----------|--------|--------|------|",
        ]
        for _, row in df.iterrows():
            lines.append(
                f"| {row.get('实施方案公告日期', '')} "
                f"| {row.get('分红类型', '')} "
                f"| {row.get('送股比例', '-')} "
                f"| {row.get('转增比例', '-')} "
                f"| {row.get('派息比例', '-')} "
                f"| {row.get('股权登记日', '-')} "
                f"| {row.get('除权日', '-')} "
                f"| {row.get('派息日', '-')} "
                f"| {row.get('实施方案分红说明', '')} |"
            )
        lines.append("")
        lines.append("## Note")
        lines.append("- 送股/转增比例单位: 股/10股")
        lines.append("- 派息比例单位: 元/10股(含税)")
        lines.append("- 数据来源: 巨潮信息网(cninfo)")
        return "\n".join(lines)
    except Exception as e:
        logger.error("get_dividends_splits_ak error: %s", e)
        return f"{_NO_DATA_PREFIX} 获取 '{ticker}' 分红送股数据失败:{e}"


# ─── 财报披露日历 (stock_yysj_em) ──────────────────────────────

def get_earnings_calendar_ak(ticker: str, curr_date: str | None = None) -> str:
    """用东方财富财报预约披露时间替代 yfinance earnings calendar。

    返回该股票最近几个报告期的财报预约/实际披露时间。
    """
    try:
        import akshare as ak

        code = _normalize_a_share_code(ticker)
        if code is None:
            return f"{_NO_DATA_PREFIX} 财报日历仅支持A股代码;got '{ticker}'."

        # 构建最近6个报告期
        if curr_date:
            try:
                curr = datetime.strptime(curr_date, "%Y-%m-%d")
            except Exception:
                curr = datetime.now()
        else:
            curr = datetime.now()

        report_periods = []
        for y in range(curr.year - 1, curr.year + 2):
            for m, d in [(3, 31), (6, 30), (9, 30), (12, 31)]:
                period = f"{y}{m:02d}{d:02d}"
                if period <= curr.strftime("%Y%m%d"):
                    report_periods.append(period)
        report_periods = sorted(report_periods)[-6:]

        results = []
        for period in report_periods:
            try:
                df = ak.stock_yysj_em(symbol="沪深A股", date=period)
                if df is not None and not df.empty:
                    row = df[df["股票代码"] == code]
                    if not row.empty:
                        results.append((period, row.iloc[0]))
            except Exception:
                continue

        if not results:
            return f"{_NO_DATA_PREFIX} 未找到 '{ticker}' 的财报披露日历数据。"

        lines = [
            f"# Earnings Calendar for {ticker} (东方财富)",
            f"# 数据源:akshare -> 东方财富",
            f"# 报告期数: {len(results)}",
            "",
            "| 报告期 | 首次预约时间 | 变更日期 | 实际披露时间 |",
            "|--------|-------------|---------|-------------|",
        ]
        for period, r in results:
            first_date = str(r.get("首次预约时间", ""))
            change1 = str(r.get("一次变更日期", ""))
            change1 = "" if change1 == "NaT" else change1
            actual = str(r.get("实际披露时间", ""))
            actual = "" if actual == "NaT" else actual
            # 报告期格式: 20251231 -> 2025年报
            period_label = period
            if period.endswith("0331"):
                period_label = f"{period[:4]}年报"
            elif period.endswith("0630"):
                period_label = f"{period[:4]}半年报"
            elif period.endswith("0930"):
                period_label = f"{period[:4]}三季报"
            elif period.endswith("1231"):
                period_label = f"{period[:4]}业绩预告"
            lines.append(f"| {period_label} | {first_date or '-'} | {change1 or '-'} | {actual or '-'} |")

        lines.append("")
        lines.append("## Note")
        lines.append("- 0331=年报, 0630=半年报, 0930=三季报, 1231=业绩预告")
        lines.append("- 实际披露时间为空表示尚未披露")
        lines.append("- 数据来源: 东方财富网")
        return "\n".join(lines)
    except Exception as e:
        logger.error("get_earnings_calendar_ak error: %s", e)
        return f"{_NO_DATA_PREFIX} 获取 '{ticker}' 财报日历数据失败:{e}"


# ─── 机构/十大股东持仓 (stock_main_stock_holder) ────────────────

def get_institutional_holders_ak(ticker: str, curr_date: str | None = None) -> str:
    """用十大股东 + 基金持仓数据替代 yfinance institutional holders。

    返回最新报告期的十大股东信息,以及持仓该股票的基金列表。
    """
    try:
        import akshare as ak

        code = _normalize_a_share_code(ticker)
        if code is None:
            return f"{_NO_DATA_PREFIX} 机构持仓数据仅支持A股代码;got '{ticker}'."

        lines = [
            f"# Institutional Holders for {ticker} (东方财富+巨潮)",
            f"# 数据源:akshare",
            "",
        ]

        # 第一部分:十大股东
        try:
            df = ak.stock_main_stock_holder(stock=code)
            if df is not None and not df.empty:
                # 按截至日期过滤
                if curr_date and "截至日期" in df.columns:
                    df["截至日期"] = df["截至日期"].astype(str)
                    df = df[df["截至日期"] <= curr_date]

                if not df.empty:
                    latest_date = str(df.iloc[0].get("截至日期", ""))
                    total_holders = str(df.iloc[0].get("股东总数", ""))
                    avg_holding = str(df.iloc[0].get("平均持股数", ""))

                    lines.append(f"## 十大股东 (截至 {latest_date})")
                    lines.append(f"股东总数: {total_holders} | 平均持股: {avg_holding}")
                    lines.append("")
                    lines.append("| 序号 | 股东名称 | 持股数量 | 持股比例 | 股本性质 |")
                    lines.append("|------|---------|---------|---------|---------|")
                    for _, row in df.iterrows():
                        lines.append(
                            f"| {row.get('编号', '')} "
                            f"| {row.get('股东名称', '')} "
                            f"| {row.get('持股数量', '')} "
                            f"| {row.get('持股比例', '')}% "
                            f"| {row.get('股本性质', '')} |"
                        )
                    lines.append("")
                else:
                    lines.append(f"## 十大股东: {curr_date} 及之前无数据")
                    lines.append("")
            else:
                lines.append("## 十大股东: 无数据")
                lines.append("")
        except Exception as e:
            lines.append(f"## 十大股东: 获取失败 ({e})")
            lines.append("")

        # 第二部分:基金持仓
        try:
            df2 = ak.stock_fund_stock_holder(symbol=code)
            if df2 is not None and not df2.empty:
                # 按截止日期过滤
                if curr_date and "截止日期" in df2.columns:
                    df2["截止日期"] = df2["截止日期"].astype(str)
                    df2 = df2[df2["截止日期"] <= curr_date]

                if not df2.empty:
                    df2 = df2.head(15)
                    lines.append(f"## 基金持仓 (Top {len(df2)})")
                    lines.append("")
                    lines.append("| 基金名称 | 基金代码 | 持仓数量 | 占流通股比例 | 持股市值 | 截止日期 |")
                    lines.append("|---------|---------|---------|------------|---------|---------|")
                    for _, row in df2.iterrows():
                        lines.append(
                            f"| {row.get('基金名称', '')} "
                            f"| {row.get('基金代码', '')} "
                            f"| {row.get('持仓数量', '')} "
                            f"| {row.get('占流通股比例', '')}% "
                            f"| {row.get('持股市值', '')} "
                            f"| {row.get('截止日期', '')} |"
                        )
                    lines.append("")
                else:
                    lines.append("## 基金持仓: 无数据")
                    lines.append("")
            else:
                lines.append("## 基金持仓: 无数据")
                lines.append("")
        except Exception as e:
            lines.append(f"## 基金持仓: 获取失败 ({e})")
            lines.append("")

        lines.append("## Note")
        lines.append("- 十大股东数据来源: 巨潮信息网")
        lines.append("- 基金持仓数据来源: 东方财富")
        lines.append("- 持股比例为百分比,单位: %")
        return "\n".join(lines)
    except Exception as e:
        logger.error("get_institutional_holders_ak error: %s", e)
        return f"{_NO_DATA_PREFIX} 获取 '{ticker}' 机构持仓数据失败:{e}"


# ─── 高管增减持 (stock_share_hold_change) ────────────────────────

def get_insider_transactions_ak(ticker: str, curr_date: str | None = None) -> str:
    """用深交所/上交所高管股份变动数据替代 yfinance insider transactions。

    返回近期董监高增减持记录,包含变动人、变动日期、变动数量、
    成交均价、变动原因等信息。
    """
    try:
        import akshare as ak

        code = _normalize_a_share_code(ticker)
        if code is None:
            return f"{_NO_DATA_PREFIX} 高管交易数据仅支持A股代码;got '{ticker}'."

        # 根据交易所选择接口
        if code.startswith("6"):
            # 沪市
            df = ak.stock_share_hold_change_sse(symbol=code)
        elif code.startswith("0") or code.startswith("3"):
            # 深市
            df = ak.stock_share_hold_change_szse(symbol=code)
        elif code.startswith("8") or code.startswith("4"):
            # 北交所 - 暂无专用接口
            return f"{_NO_DATA_PREFIX} 北交所股票 '{ticker}' 暂不支持高管交易查询。"
        else:
            return f"{_NO_DATA_PREFIX} 无法识别股票 '{ticker}' 的交易所。"

        if df is None or df.empty:
            return f"{_NO_DATA_PREFIX} 未找到 '{ticker}' 的高管股份变动记录。"

        # 日期过滤
        date_col = None
        for col in df.columns:
            if "日期" in col or "date" in col.lower():
                date_col = col
                break
        if date_col and curr_date:
            df[date_col] = df[date_col].astype(str)
            df = df[df[date_col] <= curr_date]

        if df.empty:
            return f"{_NO_DATA_PREFIX} '{ticker}' 在 {curr_date} 及之前无高管交易记录。"

        df = df.head(30)

        lines = [
            f"# Insider Transactions for {ticker} (交易所披露)",
            f"# 数据源:akshare -> 沪深交易所",
            f"# 记录数: {len(df)}",
            "",
            "| 变动日期 | 姓名 | 变动数量(万股) | 成交均价 | 变动原因 | 职务 |",
            "|---------|------|--------------|---------|---------|------|",
        ]
        for _, row in df.iterrows():
            lines.append(
                f"| {row.get('变动日期', row.get('日期', ''))} "
                f"| {row.get('董监高姓名', row.get('股份变动人姓名', ''))} "
                f"| {row.get('变动股份数量', '')} "
                f"| {row.get('成交均价', '')} "
                f"| {row.get('变动原因', '')} "
                f"| {row.get('职务', '')} |"
            )

        lines.append("")
        lines.append("## Note")
        lines.append("- 变动数量为正表示增持,为负表示减持")
        lines.append("- 成交均价单位: 元")
        lines.append("- 数据来源: 沪深交易所披露")
        return "\n".join(lines)
    except Exception as e:
        logger.error("get_insider_transactions_ak error: %s", e)
        return f"{_NO_DATA_PREFIX} 获取 '{ticker}' 高管交易数据失败:{e}"


# ─── 融资融券 (stock_margin_detail) ──────────────────────────────

def get_short_interest_ak(ticker: str, curr_date: str | None = None) -> str:
    """用融资融券数据替代 yfinance short interest。

    返回该股票的融资融券余额数据,包括融资余额、融券余量等。
    仅融资融券标的股票有数据。
    """
    try:
        import akshare as ak

        code = _normalize_a_share_code(ticker)
        if code is None:
            return f"{_NO_DATA_PREFIX} 融资融券数据仅支持A股代码;got '{ticker}'."

        # 沪市用 stock_margin_detail_sse,深市用 stock_margin_detail_szse
        market = "沪市" if code.startswith("6") else ("深市" if code.startswith("0") or code.startswith("3") else "")
        if not market:
            return f"{_NO_DATA_PREFIX} 北交所股票 '{ticker}' 暂不支持融资融券查询。"

        # 尝试获取最近30天的数据,找到最近一个交易日
        end_dt = datetime.strptime(curr_date, "%Y-%m-%d") if curr_date else datetime.now()
        results = []
        for i in range(30):
            check_date = end_dt - timedelta(days=i)
            date_str = check_date.strftime("%Y%m%d")
            try:
                df = ak.stock_margin_detail_sse(date=date_str) if market == "沪市" else ak.stock_margin_detail_szse(date=date_str)
                if df is not None and not df.empty:
                    code_col = None
                    for col in df.columns:
                        if "代码" in col or "symbol" in col.lower():
                            code_col = col
                            break
                    if code_col:
                        row = df[df[code_col] == code]
                        if not row.empty:
                            results.append((date_str, row.iloc[0]))
                            break  # 找到最近一个交易日即可
            except Exception:
                continue

        if not results:
            return f"{_NO_DATA_PREFIX} '{ticker}' 近期无融资融券数据(可能非融资融券标的)。"

        date_str, row = results[0]
        lines = [
            f"# Short Interest / Margin Trading for {ticker} (交易所)",
            f"# 数据源:akshare -> 沪深交易所",
            f"# 日期: {date_str}",
            "",
        ]

        # 输出所有可用字段
        lines.append("## 最新交易日数据")
        lines.append("")
        for col in row.index:
            val = row[col]
            if pd.notna(val):
                lines.append(f"- {col}: {val}")

        lines.append("")
        lines.append("## Note")
        lines.append("- 融资余额: 投资者借入资金买入股票的余额")
        lines.append("- 融券余量: 投资者借入股票卖出的余量")
        lines.append("- 融资买入额/融券卖出量反映当日交易活跃度")
        lines.append("- 非融资融券标的股票无数据")
        lines.append("- 数据来源: 沪深交易所")
        return "\n".join(lines)
    except Exception as e:
        logger.error("get_short_interest_ak error: %s", e)
        return f"{_NO_DATA_PREFIX} 获取 '{ticker}' 融资融券数据失败:{e}"


# ─── 新闻数据(东方财富搜索API + 财新网 + 央视新闻联播)─────────


def _fetch_eastmoney_news(code: str, page_size: int = 50) -> list[dict]:
    """通过东方财富搜索 API 获取个股新闻(比 akshare.stock_news_em 的 10 条更多)。

    直接调用 eastmoney search-api-web 接口,返回 JSON 列表。
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

    优先使用东方财富搜索 API(可获取 50 条),回退到 akshare.stock_news_em(10 条)。
    按日期范围过滤,超出范围的新闻不返回。
    """
    from datetime import datetime as _dt

    code = _normalize_a_share_code(ticker)
    if code is None:
        return (
            f"{_NO_DATA_PREFIX} 新闻搜索仅支持A股代码;"
            f"got '{ticker}'."
        )

    # 解析日期范围
    try:
        start_dt = _dt.strptime(start_date, "%Y-%m-%d")
        end_dt = _dt.strptime(end_date, "%Y-%m-%d")
    except Exception:
        start_dt = end_dt = None

    articles = []

    # 数据源 1: 东方财富搜索 API(50 条)
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
        return f"{_NO_DATA_PREFIX} 未从任何数据源找到 {ticker} 的新闻。"

    # 日期过滤:只保留 start_date ~ end_date 范围内的新闻
    # 如果无法解析日期,则保留(宁多勿缺)
    filtered = []
    for a in articles:
        if a["dt"] is None:
            filtered.append(a)
        elif start_dt and end_dt and start_dt <= a["dt"] <= end_dt:
            filtered.append(a)
        elif not start_dt or not end_dt:
            filtered.append(a)

    # 如果日期过滤后为空,使用全部新闻(带提示)
    if not filtered:
        filtered = articles
        date_note = (
            f" (注意: 未找到 {start_date} ~ {end_date} 范围内的新闻,"
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

    数据源 1: akshare.stock_news_main_cx(财新网,100 条最新财经新闻)
    数据源 2: akshare.news_cctv(央视新闻联播文字版,逐日获取)
    """
    from datetime import datetime as _dt, timedelta as _td

    try:
        curr_dt = _dt.strptime(curr_date, "%Y-%m-%d")
        start_dt = curr_dt - _td(days=look_back_days)
    except Exception as exc:
        logger.debug("get_global_news_ak date parse failed", exc_info=True)
        return f"[TOOL_ERROR] 全局新闻日期解析失败:{exc!s}"

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
                    "sort_key": 0,  # 财新新闻不知道日期,排前面
                })
    except Exception as exc:
        logger.debug("stock_news_main_cx failed: %s", exc)

    # 数据源 2: 央视新闻联播(按日期逐日获取)
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
            f"{_NO_DATA_PREFIX} 未找到指定时间范围内的全局新闻:"
            f"{start_dt.strftime('%Y-%m-%d')} and {curr_date}."
        )

    # 去重(按标题)
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

    Patch 策略(三层):
    1. yfinance 模块层:替换框架内 `tradingagents.dataflows.yfinance` 模块属性
    2. 工具模块层:通过 _patch_module_bindings() 替换已绑定的名字和 StructuredTool.func
    3. dataflows.news 层:替换 fetch_news / get_global_news_yfinance 等
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

    # 已适配的功能 - 用 akshare 数据源替代
    yf_mod.get_analyst_ratings = get_analyst_ratings_ak
    yf_mod.get_earnings_calendar = get_earnings_calendar_ak
    yf_mod.get_institutional_holders = get_institutional_holders_ak
    yf_mod.get_insider_transactions = get_insider_transactions_ak
    yf_mod.get_short_interest = get_short_interest_ak
    yf_mod.get_dividends_splits = get_dividends_splits_ak

    yf_mod._resolve_history_with_cache = _resolve_history_with_cache_ak

    # ── 第二层:patch 工具模块中已绑定的名字 + StructuredTool.func ──
    try:
        import tradingagents.agents.utils.fundamental_data_tools as fd_tools
        # 第一轮:patch _get_xxx 别名(plain function 绑定)
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
        # 第二轮:patch @tool 装饰的 StructuredTool 的 .func(不改模块属性,只修原对象)
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
        # 注意:不要替换 get_indicators 的 func!
        # get_indicators 的签名是 indicator (单数 str),内部会转换成 indicators 列表
        # 再调用 get_stock_stats_indicators_batch(symbol, indicators, ...)
        # _patch_module_bindings 已经把 ti_tools 模块里的 get_stock_stats_indicators_batch
        # 引用替换成了 akshare 版本,所以 get_indicators 内部调用会正确走到 akshare 实现。
        # 之前把 get_indicators.func 直接替换成 get_stock_stats_indicators_batch 导致
        # 参数名不匹配 (indicator vs indicators),LLM 传入 indicator=xxx 就报错了。
        logger.info("  → technical_indicators_tools patched")
    except Exception as e:
        logger.warning("  → technical_indicators_tools patch skipped: %s", e)

    logger.info("akshare adapter fully applied")


# 自动应用 patch(import 本模块即生效)
apply_patch()
