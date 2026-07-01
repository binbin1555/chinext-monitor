"""
生成 docs/data.json（仪表盘数据）。
HTML 模板在 docs/index.html（静态，由 git 维护），本脚本只更新数据 JSON。
"""
import json
import logging
import numpy as np
import pandas as pd
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

NaN_SAFE = lambda v: None if (v is None or (isinstance(v, float) and np.isnan(v))) else v


def generate_data_json(
    hist: pd.DataFrame,
    state: dict,
    ledger: pd.DataFrame,
    config: dict,
    metrics_perf: dict,
    nav_df: pd.DataFrame,
    gaps: list,
    warnings: list,
    docs_dir: Path,
):
    """将所有仪表盘所需数据序列化为 docs/data.json。"""

    start_date = config.get("start_date", "2023-01-01")
    # 仪表盘只展示 start_date 以来的数据（约 3 年），避免数据量过大
    view = hist[hist["date"] >= start_date].copy().reset_index(drop=True)

    dates     = view["date"].tolist()
    close_arr = [NaN_SAFE(v) for v in view["close"].tolist()]
    ma20_arr  = [NaN_SAFE(v) for v in view["ma20"].tolist()]
    ma60_arr  = [NaN_SAFE(v) for v in view["ma60"].tolist()]
    ma120_arr = [NaN_SAFE(v) for v in view["ma120"].tolist()]
    pb_arr    = [NaN_SAFE(v) for v in view["pb"].tolist()]
    pe_arr    = [NaN_SAFE(v) for v in view["pe_ttm"].tolist()]
    pb_pct    = [NaN_SAFE(v) for v in view["pb_pct10y"].tolist()]
    pe_pct    = [NaN_SAFE(v) for v in view["pe_pct10y"].tolist()]
    temp300   = [NaN_SAFE(v) for v in view["temp_300"].tolist()]
    temp500   = [NaN_SAFE(v) for v in view["temp_500"].tolist()]
    close300  = [NaN_SAFE(v) for v in view["close_300"].tolist()]

    # ── 净值序列（已由 metrics 计算） ──
    if nav_df is not None and len(nav_df) > 0:
        nav_sub = nav_df[nav_df["date"].isin(dates)].set_index("date")
        nav_port = [NaN_SAFE(nav_sub.loc[d, "nav_portfolio"]) if d in nav_sub.index else None for d in dates]
        nav_bh   = [NaN_SAFE(nav_sub.loc[d, "nav_buy_hold"])  if d in nav_sub.index else None for d in dates]
        nav_bm   = [NaN_SAFE(nav_sub.loc[d, "nav_benchmark"]) if d in nav_sub.index else None for d in dates]
    else:
        nav_port = nav_bh = nav_bm = [None] * len(dates)

    # ── 买卖标记（来自 ledger） ──
    buy_signals    = []
    reduce_signals = []
    exit_signals   = []
    if ledger is not None and len(ledger) > 0:
        for _, row in ledger.iterrows():
            d = str(row["date"])
            if d not in dates:
                continue
            close_at = view.loc[view["date"] == d, "close"].values
            price    = float(close_at[0]) if len(close_at) else float(row["price"])
            action   = str(row["action"]).lower()
            if action == "buy":
                buy_signals.append({
                    "date": d, "value": price,
                    "fen": int(row["fen"]),
                    "note": str(row.get("note", "")).replace('"', ""),
                })
            elif action == "reduce":
                reduce_signals.append({"date": d, "value": price})
            elif action == "exit":
                exit_signals.append({"date": d, "value": price})

    # ── 当前状态摘要 ──
    # th = 策略理论账本（驱动信号/缺口）；ls = 用户实盘账本（仅显示实际盈亏，不参与信号）
    from src.engine import recalc_from_ledger, theoretical_position
    th = theoretical_position(state, config.get("total_fen", 150))
    ls = recalc_from_ledger(ledger, config.get("total_fen", 150),
                            state.get("cycle_start_date"))
    today_row = hist.iloc[-1] if len(hist) > 0 else None
    cur_close = float(today_row["close"]) if today_row is not None and not pd.isna(today_row["close"]) else None
    def _fp(avg):
        return round(cur_close / avg - 1, 4) if avg and cur_close else None
    float_profit = _fp(th.get("weighted_avg"))   # 策略理论浮盈（驱动信号）
    actual_fp    = _fp(ls.get("weighted_avg"))   # 用户实盘浮盈（仅显示）

    phase_label = {
        "waiting": "空仓等待",
        "holding": "建仓 / 持有中",
    }.get(state.get("phase", ""), state.get("phase", ""))

    next_action = _next_action_hint(state, th, today_row, config)

    data = {
        "last_update": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "start_date":  start_date,
        "dates":       dates,
        "close":       close_arr,
        "ma20":        ma20_arr,
        "ma60":        ma60_arr,
        "ma120":       ma120_arr,
        "pb":          pb_arr,
        "pe_ttm":      pe_arr,
        "pb_pct10y":   pb_pct,
        "pe_pct10y":   pe_pct,
        "temp_300":    temp300,
        "temp_500":    temp500,
        "close_300":   close300,
        "nav_portfolio": nav_port,
        "nav_buy_hold":  nav_bh,
        "nav_benchmark": nav_bm,
        "buy_signals":    buy_signals,
        "reduce_signals": reduce_signals,
        "exit_signals":   exit_signals,
        "status": {
            "phase":              state.get("phase", "waiting"),
            "phase_label":        phase_label,
            "next_action":        next_action,
            "current_fen":        th.get("current_fen", 0),
            "total_fen":          config.get("total_fen", 150),
            "total_bought":       th.get("total_bought", 0),
            "weighted_avg":       round(th["weighted_avg"], 2) if th.get("weighted_avg") else None,
            "current_close":      cur_close,
            "float_profit":       float_profit,
            # 用户实盘（仅供显示，不参与任何信号判断）
            "actual_current_fen":  ls.get("current_fen", 0),
            "actual_total_bought": ls.get("total_bought", 0),
            "actual_weighted_avg": round(ls["weighted_avg"], 2) if ls.get("weighted_avg") else None,
            "actual_float_profit": actual_fp,
            "armed":              state.get("armed", False),
            "observation_entered": state.get("observation_entered", False),
            "exit_streak":        state.get("exit_streak", 0),
            "t1_fired":           state.get("t1_fired", False),
            "t2_fired":           state.get("t2_fired", False),
            "t3_fired":           state.get("t3_fired", False),
            "pending_signals":      len(state.get("signals_pending", [])),
            "pending_signals_list": state.get("signals_pending", []),
            "initial_capital":      float(config.get("initial_capital", 2_000_000)),
        },
        "metrics": {
            "cagr":         _round4(metrics_perf.get("cagr")),
            "max_drawdown": _round4(metrics_perf.get("max_drawdown")),
            "annual_vol":   _round4(metrics_perf.get("annual_vol")),
            "sharpe":       _round4(metrics_perf.get("sharpe")),
        },
        "gaps":     gaps,
        "warnings": warnings,
        "committed_ledger": _export_ledger(ledger),
        "trigger_log":      state.get("trigger_log", []),
    }

    docs_dir.mkdir(parents=True, exist_ok=True)
    out_path = docs_dir / "data.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    logger.info(f"data.json 已生成，{len(dates)} 个日期点")


def _export_ledger(ledger) -> list:
    if ledger is None or len(ledger) == 0:
        return []
    rows = []
    for i, row in ledger.iterrows():
        rows.append({
            "id":     f"committed_{i}",
            "date":   str(row["date"]),
            "action": str(row["action"]),
            "fen":    int(row["fen"]),
            "price":  float(row["price"]),
            "note":   str(row.get("note", "") or ""),
        })
    return rows


def _round4(v):
    return round(v, 4) if v is not None and not (isinstance(v, float) and np.isnan(v)) else None


def _next_action_hint(state, ledger_state, today_row, config) -> str:
    if today_row is None:
        return "等待数据"
    pb_pct = NaN_SAFE(today_row.get("pb_pct10y") if hasattr(today_row, "get") else today_row["pb_pct10y"])
    phase  = state.get("phase", "waiting")

    if phase == "waiting":
        if pb_pct is not None and pb_pct < 0.20:
            return "PB分位已低于20%，等待T1触发"
        return f"空仓观望，等待PB分位<20%（当前{pb_pct*100:.1f}%）" if pb_pct else "等待低估机会"

    fp = None
    if ledger_state.get("weighted_avg") and today_row is not None:
        close = NaN_SAFE(today_row["close"] if hasattr(today_row, "__getitem__") else None)
        if close:
            fp = close / ledger_state["weighted_avg"] - 1

    if state.get("armed"):
        if state.get("exit_streak", 0) >= 1:
            return f"⚠️ 止盈条件满足中（已{state['exit_streak']}天/需3天）"
        return "已武装，关注MA120方向"
    if fp is not None:
        return f"持有中，浮盈{fp*100:.1f}%，关注估值与均线"
    return "持有中，等待止盈条件"
