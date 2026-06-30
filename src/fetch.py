"""
数据抓取层：理杏仁（K线 + 基本面）+ 且慢温度计 MCP。
负责拉取历史数据、增量更新、重试/回退、缓存到 data/history.csv。

history.csv 列：
  date, close, pb, pe_ttm, pb_pct10y, pe_pct10y,
  ma20, ma60, ma120, close_300, temp_300, temp_500
"""
import os
import json
import time
import logging
import requests
import pandas as pd
import numpy as np
from datetime import date, timedelta, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

LIXINGER_URL        = "https://open.lixinger.com/api"
LIXINGER_FUND_URL   = "https://open.lixinger.com/api/cn/index/fundamental"
QIEMAN_URL          = "https://stargate.yingmi.com/mcp/v2"

# 理杏仁必须在 headers 里声明 Content-Type 和 Accept-Encoding: gzip
LIXINGER_HEADERS = {
    "Content-Type": "application/json",
    "Accept-Encoding": "gzip, deflate, br",
}

HISTORY_COLS = [
    "date", "close", "pb", "pe_ttm",
    "pb_pct10y", "pe_pct10y",
    "ma20", "ma60", "ma120",
    "close_300", "temp_300", "temp_500",
]


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────

def _post_with_retry(url, json_body, headers=None, retries=3, timeout=45):
    """带指数退避的 POST 请求。"""
    merged_headers = {**LIXINGER_HEADERS, **(headers or {})}
    for attempt in range(retries):
        try:
            r = requests.post(url, json=json_body, headers=merged_headers, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            wait = 2 ** attempt
            logger.warning(f"请求失败（第{attempt+1}次）: {e}，{wait}s 后重试")
            if attempt < retries - 1:
                time.sleep(wait)
    raise RuntimeError(f"请求失败（已重试{retries}次）: {url}")


def _parse_lixinger_date(d_str):
    """将理杏仁返回的 ISO 8601 日期字符串转为 YYYY-MM-DD。"""
    return d_str[:10]


# ─────────────────────────────────────────────
# 理杏仁 API
# ─────────────────────────────────────────────

def fetch_lixinger_close(token, stock_code, start_date, end_date):
    """
    用 fundamental 接口拉取指数收盘点位（cp），返回 {date_str: close}。
    注意：使用 startDate 时 stockCodes 只能传一个代码。
    """
    body = {
        "token": token,
        "stockCodes": [stock_code],
        "startDate": start_date,
        "endDate": end_date,
        "metricsList": ["cp"],
    }
    r = _post_with_retry(LIXINGER_FUND_URL, body)
    resp = r.json()
    if resp.get("code") not in (1, 200) or not resp.get("data"):
        raise RuntimeError(f"理杏仁 cp 接口异常({stock_code}): {resp.get('message', resp)}")
    result = {}
    for item in resp["data"]:
        d = _parse_lixinger_date(item.get("date", ""))
        cp = item.get("cp")
        result[d] = float(cp) if cp is not None else None
    return result


def fetch_lixinger_fundamental(token, stock_code, start_date, end_date):
    """
    拉取创业板指 PE/PB 当前值 + 近10年分位点（cvpos），以及收盘点位。
    分位点直接使用理杏仁官方计算（基于其完整10年数据库），无需本地重算。
    返回 {date_str: {close, pb, pe_ttm, pb_pct10y, pe_pct10y}}。
    """
    body = {
        "token": token,
        "stockCodes": [stock_code],
        "startDate": start_date,
        "endDate": end_date,
        "metricsList": [
            "cp",
            "pe_ttm.y10.mcw.cv",
            "pe_ttm.y10.mcw.cvpos",
            "pb.y10.mcw.cv",
            "pb.y10.mcw.cvpos",
        ],
    }
    r = _post_with_retry(LIXINGER_FUND_URL, body)
    resp = r.json()
    if resp.get("code") not in (1, 200) or not resp.get("data"):
        raise RuntimeError(f"理杏仁 基本面接口异常: {resp.get('message', resp)}")

    result = {}
    for item in resp["data"]:
        d = _parse_lixinger_date(item.get("date", ""))
        def _f(key):
            v = item.get(key)
            return float(v) if v is not None else None
        result[d] = {
            "close":     _f("cp"),
            "pb":        _f("pb.y10.mcw.cv"),
            "pe_ttm":    _f("pe_ttm.y10.mcw.cv"),
            "pb_pct10y": _f("pb.y10.mcw.cvpos"),
            "pe_pct10y": _f("pe_ttm.y10.mcw.cvpos"),
        }
    return result


def _extract_nested(item, key, sub):
    """兼容 {key: {sub: v}} 和 {"key.sub": v} 两种格式。"""
    if key in item and isinstance(item[key], dict):
        return item[key].get(sub)
    flat_key = f"{key}.{sub}"
    if flat_key in item:
        return item[flat_key]
    return None


# ─────────────────────────────────────────────
# 且慢温度计 MCP（Streamable HTTP）
# ─────────────────────────────────────────────

class QiemanMCP:
    """最小 MCP 客户端，支持 JSON-RPC over HTTP（+ SSE 解析）。"""

    def __init__(self, api_key):
        self.api_key = api_key
        self.headers = {
            "x-api-key": api_key,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        self._initialized = False

    def _post(self, body, stream=False):
        return _post_with_retry(QIEMAN_URL, body, headers=self.headers)

    def initialize(self):
        body = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
            },
            "id": 1,
        }
        try:
            r = requests.post(QIEMAN_URL, json=body, headers=self.headers, timeout=30)
            self._initialized = True
            return r.json()
        except Exception as e:
            logger.warning(f"且慢 initialize 失败（非关键）: {e}")
            self._initialized = True  # 即使握手失败也继续尝试 tools/call

    def get_temperature(self, cal_date: str):
        """
        调用 GetLatestQuotations，返回 {temp_300: float|None, temp_500: float|None}。
        非交易日或失败时返回 None 值，不中断主流程。
        """
        if not self._initialized:
            self.initialize()

        body = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "GetLatestQuotations",
                "arguments": {"calDate": cal_date},
            },
            "id": 2,
        }

        try:
            r = requests.post(
                QIEMAN_URL, json=body, headers=self.headers,
                timeout=30, stream=True
            )
            content_type = r.headers.get("Content-Type", "")
            raw_text = ""

            if "event-stream" in content_type:
                for line in r.iter_lines():
                    if line:
                        s = line if isinstance(line, str) else line.decode("utf-8")
                        if s.startswith("data:"):
                            raw_text = s[5:].strip()
                            break
            else:
                raw_text = r.text

            return self._parse_temperature(raw_text)

        except Exception as e:
            logger.warning(f"且慢温度获取失败 {cal_date}: {e}")
            return {"temp_300": None, "temp_500": None}

    @staticmethod
    def _parse_temperature(raw_text):
        """解析 JSON-RPC result，提取 CSI300 和 CSI500 温度。"""
        out = {"temp_300": None, "temp_500": None}
        try:
            obj = json.loads(raw_text)
            # 可能是 result.content[0].text 里嵌套的 JSON
            content = None
            if "result" in obj:
                res = obj["result"]
                if isinstance(res, dict) and "content" in res:
                    for c in res["content"]:
                        if c.get("type") == "text":
                            content = json.loads(c["text"])
                            break
                elif isinstance(res, dict) and "temperatureList" in res:
                    content = res
            if not content:
                content = obj  # 直接就是数据

            temp_list = content.get("temperatureList", [])
            for item in temp_list:
                code = item.get("temperatureIndexCode") or item.get("indexCode", "")
                temp = item.get("temperature")
                if code == "000300":
                    out["temp_300"] = float(temp) if temp is not None else None
                elif code == "000905":
                    out["temp_500"] = float(temp) if temp is not None else None
        except Exception as e:
            logger.debug(f"温度解析异常: {e}  raw={raw_text[:200]}")
        return out


# ─────────────────────────────────────────────
# 种子数据加载（使用已有本地 CSV）
# ─────────────────────────────────────────────

def _strip_eq(v):
    """去掉 Excel 公式前缀的 =。"""
    if isinstance(v, str) and v.startswith("="):
        return v[1:]
    return v


def load_seed_from_local(data_dir: Path):
    """
    从 data_dir 父目录中已有的理杏仁导出 CSV 构建历史数据 DataFrame。
    返回按日期升序排列的 DataFrame，列同 HISTORY_COLS（温度列为 NaN）。
    """
    base = data_dir.parent  # 创业板原始数据/

    # ── PB ──
    pb_files = sorted(base.glob("创业板指_PB_市值加权_10年_*.csv"))
    pe_files = sorted(base.glob("创业板指_PE-TTM_市值加权_10年_*.csv"))
    ma120_files = sorted(base.glob("创业板近10年均线_MA120.csv"))
    ma60_files  = sorted(base.glob("创业板近10年均线_MA60.csv"))
    ma20_files  = sorted(base.glob("创业板近10年均线_MA20.csv"))

    if not pb_files:
        return None

    def read_csv_clean(path, date_col="日期"):
        df = pd.read_csv(path, dtype=str)
        df.columns = [c.strip().strip('"').strip("=") for c in df.columns]
        for col in df.columns:
            if col != date_col:
                df[col] = df[col].apply(_strip_eq).apply(
                    lambda x: pd.to_numeric(x, errors="coerce")
                )
        # 统一日期格式
        df[date_col] = df[date_col].str.replace("/", "-").str.strip()
        df[date_col] = pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d")
        return df.dropna(subset=[date_col])

    # PB
    pb_df = read_csv_clean(pb_files[-1])
    pb_cols = {c: c for c in pb_df.columns}
    close_col = next((c for c in pb_df.columns if "收盘" in c), None)
    pb_col    = next((c for c in pb_df.columns if c.upper().startswith("PB市值") or c.startswith("PB市值")), None)
    if pb_col is None:
        pb_col = next((c for c in pb_df.columns if "PB" in c.upper() and "分位" not in c), None)

    # 理杏仁官方近10年分位列（"PB 分位点"，排除 "PB 80%分位点值" 等阈值列）
    pb_pct_col = next((c for c in pb_df.columns
                       if "分位点" in c and "%" not in c and "值" not in c), None)

    keep = ["日期", close_col, pb_col] + ([pb_pct_col] if pb_pct_col else [])
    merged = pb_df[keep].rename(
        columns={"日期": "date", close_col: "close", pb_col: "pb",
                 **({pb_pct_col: "pb_pct10y"} if pb_pct_col else {})}
    )
    if not pb_pct_col:
        merged["pb_pct10y"] = np.nan

    # PE
    if pe_files:
        pe_df = read_csv_clean(pe_files[-1])
        pe_col = next((c for c in pe_df.columns if "PE" in c.upper() and "分位" not in c and "市值" in c), None)
        pe_pct_col = next((c for c in pe_df.columns
                           if "分位点" in c and "%" not in c and "值" not in c), None)
        pe_keep = ["日期"] + ([pe_col] if pe_col else []) + ([pe_pct_col] if pe_pct_col else [])
        if len(pe_keep) > 1:
            pe_sub = pe_df[pe_keep].rename(
                columns={"日期": "date",
                         **({pe_col: "pe_ttm"} if pe_col else {}),
                         **({pe_pct_col: "pe_pct10y"} if pe_pct_col else {})}
            )
            merged = merged.merge(pe_sub, on="date", how="left")
        if "pe_ttm" not in merged.columns:
            merged["pe_ttm"] = np.nan
        if "pe_pct10y" not in merged.columns:
            merged["pe_pct10y"] = np.nan
    else:
        merged["pe_ttm"] = np.nan
        merged["pe_pct10y"] = np.nan

    # MA
    def merge_ma(files, col_name):
        if not files:
            return
        nonlocal merged
        ma_df = read_csv_clean(files[-1])
        ma_col = next((c for c in ma_df.columns if "MA" in c.upper()), None)
        if ma_col:
            sub = ma_df[["日期", ma_col]].rename(columns={"日期": "date", ma_col: col_name})
            merged = merged.merge(sub, on="date", how="left")
        else:
            merged[col_name] = np.nan

    merge_ma(ma20_files, "ma20")
    merge_ma(ma60_files, "ma60")
    merge_ma(ma120_files, "ma120")

    # 空列补齐（分位列已从理杏仁官方"分位点"列读入，不再清空）
    for col in ["close_300", "temp_300", "temp_500"]:
        merged[col] = np.nan

    merged = merged.sort_values("date").drop_duplicates("date")
    return merged[HISTORY_COLS]


# ─────────────────────────────────────────────
# 分位计算
# ─────────────────────────────────────────────

WINDOW_DAYS = 2430  # ≈ 10 年交易日


def compute_percentiles(df: pd.DataFrame) -> pd.DataFrame:
    """在 df 上原地计算 pb_pct10y 和 pe_pct10y（0~1）。"""
    df = df.copy()
    pb_arr = df["pb"].values.astype(float)
    pe_arr = df["pe_ttm"].values.astype(float)
    n = len(df)
    pb_pct = np.full(n, np.nan)
    pe_pct = np.full(n, np.nan)

    for i in range(n):
        start = max(0, i + 1 - WINDOW_DAYS)
        window_pb = pb_arr[start : i + 1]
        window_pe = pe_arr[start : i + 1]

        valid_pb = window_pb[~np.isnan(window_pb)]
        if len(valid_pb) >= 60:
            pb_pct[i] = float(np.sum(valid_pb <= valid_pb[-1])) / len(valid_pb)

        valid_pe = window_pe[~np.isnan(window_pe)]
        if len(valid_pe) >= 60:
            pe_pct[i] = float(np.sum(valid_pe <= valid_pe[-1])) / len(valid_pe)

    df["pb_pct10y"] = pb_pct
    df["pe_pct10y"] = pe_pct
    return df


# ─────────────────────────────────────────────
# 均线计算
# ─────────────────────────────────────────────

def compute_mas(df: pd.DataFrame) -> pd.DataFrame:
    """若 CSV 中均线有缺失，用收盘价补算。"""
    df = df.copy()
    close = pd.to_numeric(df["close"], errors="coerce")
    if df["ma20"].isna().any():
        df["ma20"] = close.rolling(20, min_periods=20).mean()
    if df["ma60"].isna().any():
        df["ma60"] = close.rolling(60, min_periods=60).mean()
    if df["ma120"].isna().any():
        df["ma120"] = close.rolling(120, min_periods=120).mean()
    return df


# ─────────────────────────────────────────────
# 主更新函数
# ─────────────────────────────────────────────

def update_history(config: dict, data_dir: Path):
    """
    全量更新 data/history.csv：
    1. 若文件不存在，从本地 CSV 种子加载并补拉理杏仁 + 分位。
    2. 若文件存在，只拉取缺失的最新日期。
    3. 追加今日且慢温度。
    """
    token  = os.environ.get("LIXINGER_TOKEN", "")
    qkey   = os.environ.get("QIEMAN_KEY", "")
    csv_path = data_dir / "history.csv"

    # ── 加载现有数据 ──
    if csv_path.exists():
        hist = pd.read_csv(csv_path, dtype={"date": str})
        logger.info(f"加载已有历史 {len(hist)} 行，最新日期 {hist['date'].max()}")
    else:
        logger.info("history.csv 不存在，从本地 CSV 种子初始化…")
        hist = load_seed_from_local(data_dir)
        if hist is None:
            hist = pd.DataFrame(columns=HISTORY_COLS)
            logger.warning("本地 CSV 种子未找到，将从理杏仁全量拉取")

    hist = hist.sort_values("date").drop_duplicates("date").reset_index(drop=True)

    # ── 确定需要拉取的日期范围 ──
    today_str = date.today().strftime("%Y-%m-%d")
    if len(hist) > 0:
        last_date = hist["date"].max()
        fetch_start = (
            datetime.strptime(last_date, "%Y-%m-%d") + timedelta(days=1)
        ).strftime("%Y-%m-%d")
    else:
        years = config.get("history_years", 11)
        fetch_start = (date.today() - timedelta(days=years * 366)).strftime("%Y-%m-%d")
    fetch_end = today_str

    # ── 从理杏仁拉取增量（若有 token） ──
    if token and fetch_start <= fetch_end:
        logger.info(f"从理杏仁拉取 {fetch_start} ~ {fetch_end}")
        try:
            chinext_code = config.get("chinext_code", "399006")
            bench_code   = config.get("benchmark_code", "000300")

            # 基本面接口一次获取 close + pe_ttm + pb（399006）
            fundam = fetch_lixinger_fundamental(token, chinext_code, fetch_start, fetch_end)
            # 单独拉沪深300收盘价用于均线参考
            c300   = fetch_lixinger_close(token, bench_code, fetch_start, fetch_end)

            all_dates = sorted(fundam.keys())
            new_rows = []
            for d in all_dates:
                f = fundam[d]
                row = {
                    "date":      d,
                    "close":     f.get("close"),
                    "pb":        f.get("pb"),
                    "pe_ttm":    f.get("pe_ttm"),
                    "pb_pct10y": f.get("pb_pct10y"),   # 理杏仁官方近10年分位
                    "pe_pct10y": f.get("pe_pct10y"),   # 理杏仁官方近10年分位
                    "ma20":      np.nan,
                    "ma60":      np.nan,
                    "ma120":     np.nan,
                    "close_300": c300.get(d),
                    "temp_300":  np.nan,
                    "temp_500":  np.nan,
                }
                new_rows.append(row)

            if new_rows:
                new_df = pd.DataFrame(new_rows, columns=HISTORY_COLS)
                hist = pd.concat([hist, new_df], ignore_index=True)
                hist = hist.sort_values("date").drop_duplicates("date").reset_index(drop=True)
                logger.info(f"新增 {len(new_rows)} 条理杏仁数据")

        except Exception as e:
            logger.error(f"理杏仁拉取失败，使用缓存: {e}")

    # ── 重算均线；分位优先用理杏仁 cvpos，仅对种子数据中的 NaN 行本地兜底 ──
    hist = compute_mas(hist)
    if hist["pb_pct10y"].isna().any():
        logger.info("部分行缺少分位数据，执行本地兜底计算…")
        hist = compute_percentiles(hist)

    # ── 更新今日温度（若有 key） ──
    if qkey:
        trade_dates = hist["date"].tolist()
        today_str2 = date.today().strftime("%Y-%m-%d")
        dates_to_fetch = [d for d in trade_dates if pd.isna(
            hist.loc[hist["date"] == d, "temp_300"].values[0]
            if len(hist.loc[hist["date"] == d]) > 0 else None
        )]
        # 只更新最近 30 天缺失的温度
        recent_missing = [d for d in dates_to_fetch if d >= (
            date.today() - timedelta(days=45)).strftime("%Y-%m-%d")]
        if recent_missing:
            qm = QiemanMCP(qkey)
            for d in recent_missing[-20:]:  # 每次最多补 20 天
                temps = qm.get_temperature(d)
                if temps["temp_300"] is not None:
                    idx = hist.index[hist["date"] == d]
                    if len(idx):
                        hist.loc[idx, "temp_300"] = temps["temp_300"]
                        hist.loc[idx, "temp_500"] = temps["temp_500"]
                        logger.info(f"温度 {d}: 300={temps['temp_300']} 500={temps['temp_500']}")

    # ── 保存 ──
    data_dir.mkdir(parents=True, exist_ok=True)
    hist.to_csv(csv_path, index=False, float_format="%.4f")
    logger.info(f"history.csv 已保存，共 {len(hist)} 行")
    return hist
