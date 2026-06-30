"""
指标计算：净值曲线、年化收益、最大回撤、夏普、距触发缺口。
"""
import numpy as np
import pandas as pd
from datetime import datetime


def build_nav_series(hist: pd.DataFrame, ledger: pd.DataFrame,
                     config: dict, state: dict) -> pd.DataFrame:
    """
    构建每日净值序列，返回 DataFrame：
      date, nav_portfolio, nav_buy_hold, nav_benchmark
    均从 start_date 当日收盘开始归一化为 1.0。
    """
    start_date    = config.get("start_date", "2023-01-01")
    initial_cap   = float(config.get("initial_capital", 2_000_000))
    cash_rate     = float(config.get("cash_rate_annual", 0.02))
    total_fen     = int(config.get("total_fen", 150))

    sub = hist[hist["date"] >= start_date].copy().reset_index(drop=True)
    if len(sub) == 0:
        return pd.DataFrame(columns=["date", "nav_portfolio", "nav_buy_hold", "nav_benchmark"])

    dates       = sub["date"].tolist()
    close_arr   = sub["close"].values.astype(float)
    close300    = sub["close_300"].values.astype(float)

    # 锚定基准收盘
    start_close     = close_arr[0]
    start_close_300 = close300[0] if not np.isnan(close300[0]) else None

    # ── 组合净值 ──
    cash       = initial_cap
    holdings   = 0.0   # 持有点位份额（元/点）
    nav_port   = []

    # 按日期将流水映射
    ledger_by_date = {}
    if ledger is not None and len(ledger) > 0:
        for _, lrow in ledger.iterrows():
            d = str(lrow["date"])
            ledger_by_date.setdefault(d, []).append(lrow)

    for i, d in enumerate(dates):
        close_i = close_arr[i]
        if np.isnan(close_i):
            nav_port.append(nav_port[-1] if nav_port else 1.0)
            continue

        # 处理当日流水
        for lrow in ledger_by_date.get(d, []):
            action = str(lrow["action"]).strip().lower()
            fen    = int(lrow["fen"])
            price  = float(lrow["price"])
            amount = fen * (initial_cap / total_fen)  # 每份金额
            shares = amount / price                    # 购入的指数份额（点为单位）

            if action == "buy":
                cash     -= amount
                holdings += shares
            elif action in ("reduce", "exit"):
                proceeds  = shares * price  # 实际卖出按当时流水价
                # 按比例卖出
                sell_shares = holdings * (fen / max(1, _current_holding_fen(ledger, d)))
                proceeds    = sell_shares * price
                cash       += proceeds
                holdings   -= sell_shares

        # 利息（日化）
        daily_rate = (1 + cash_rate) ** (1 / 252) - 1
        cash *= (1 + daily_rate)

        portfolio_value = cash + holdings * close_i
        nav_port.append(portfolio_value / initial_cap)

    # ── 买入持有净值 ──
    nav_bh = [close_arr[i] / start_close for i in range(len(dates))]

    # ── 基准（沪深300）净值 ──
    if start_close_300 is not None and not np.isnan(start_close_300):
        nav_bm = [
            (close300[i] / start_close_300 if not np.isnan(close300[i]) else None)
            for i in range(len(dates))
        ]
    else:
        nav_bm = [None] * len(dates)

    result = pd.DataFrame({
        "date":          dates,
        "nav_portfolio": nav_port,
        "nav_buy_hold":  nav_bh,
        "nav_benchmark": nav_bm,
    })
    return result


def _current_holding_fen(ledger: pd.DataFrame, as_of_date: str) -> int:
    """截至 as_of_date，持有的总份数（买 - 减 - 清）。"""
    sub = ledger[ledger["date"] <= as_of_date]
    return max(0, int(sub[sub["action"] == "buy"]["fen"].sum())
               - int(sub[sub["action"].isin(["reduce", "exit"])]["fen"].sum()))


def calc_performance(nav_series: pd.Series, start_date: str,
                     risk_free_rate: float = 0.02) -> dict:
    """
    计算 CAGR、最大回撤、年化波动率、夏普比率。
    nav_series: 按日的净值序列（起始为 1.0）。
    """
    arr = np.array([x for x in nav_series if x is not None and not np.isnan(float(x))],
                   dtype=float)
    if len(arr) < 2:
        return dict(cagr=None, max_drawdown=None, annual_vol=None, sharpe=None)

    # CAGR
    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt   = datetime.now()
        years    = (end_dt - start_dt).days / 365.25
        cagr     = float(arr[-1] ** (1 / max(years, 0.01)) - 1) if years > 0 else None
    except Exception:
        cagr = None

    # 最大回撤
    peak    = np.maximum.accumulate(arr)
    dd      = arr / peak - 1.0
    max_dd  = float(dd.min())

    # 年化波动率
    daily_ret = np.diff(arr) / arr[:-1]
    annual_vol = float(daily_ret.std() * np.sqrt(252)) if len(daily_ret) > 1 else None

    # 夏普
    if annual_vol and annual_vol > 0 and cagr is not None:
        sharpe = float((cagr - risk_free_rate) / annual_vol)
    else:
        sharpe = None

    return dict(
        cagr=cagr,
        max_drawdown=max_dd,
        annual_vol=annual_vol,
        sharpe=sharpe,
    )


def calc_gaps(hist: pd.DataFrame, today_str: str, state: dict,
              ledger: pd.DataFrame, config: dict) -> list:
    """
    计算每个未触发条件的"距触发还差多少"，返回结构化描述列表供仪表盘展示。

    每个 gap 字段：
      name      触发类型短标签（如"减仓 50%"）
      big       高亮数字（如"+4.9%"、"744 点"、"2 天"）
      headline  动作描述句（如"点位再涨 4.9%，触发减仓 50%"）
      note      次要说明
      current   当前状态行
      progress  0~1，越接近 1 越快触发（前端用它挑选"最快触发"的一项）
      tone      up=需上涨(红) / down=需下跌(绿) / warn=临界(橙) / neutral
    """
    gaps = []
    row = hist[hist["date"] == today_str]
    if len(row) == 0:
        return gaps
    row = row.iloc[0]

    pb_pct  = row["pb_pct10y"] if not pd.isna(row["pb_pct10y"]) else None
    close   = row["close"]     if not pd.isna(row["close"])     else None
    ma120   = row["ma120"]     if not pd.isna(row["ma120"])     else None
    total_fen = config.get("total_fen", 150)

    ls = {}
    if ledger is not None and len(ledger) > 0:
        from src.engine import recalc_from_ledger
        ls = recalc_from_ledger(ledger, total_fen)
    fp = (float(close) / ls.get("weighted_avg") - 1
          if ls.get("weighted_avg") and close else None)

    def clamp01(x):
        return round(max(0.0, min(1.0, x)), 3)

    # ── 买入侧 T1 / T2 / T3 ──
    buy_levels = [
        ("t1_fired", 0.20, "T1 加仓"),
        ("t2_fired", 0.15, "T2 加仓"),
        ("t3_fired", 0.10, "T3 满仓"),
    ]
    if pb_pct is not None:
        for flag, thr, label in buy_levels:
            if state.get(flag):
                continue
            if pb_pct >= thr:
                drop_pp = (pb_pct - thr) * 100
                gaps.append({
                    "name": label,
                    "big": f"{drop_pp:.1f}%",
                    "headline": f"PB 分位再降 {drop_pp:.1f}%，触发{label}",
                    "note": "",
                    "current": f"当前 PB 分位 {pb_pct*100:.1f}%，目标 ≤{thr*100:.0f}%",
                    "progress": clamp01(1 - (pb_pct - thr) / (1 - thr)),
                    "tone": "down",
                })
            else:
                gaps.append({
                    "name": label,
                    "big": "已满足",
                    "headline": f"PB 分位已低于 {thr*100:.0f}%，可执行{label}",
                    "note": "",
                    "current": f"当前 PB 分位 {pb_pct*100:.1f}%",
                    "progress": 1.0,
                    "tone": "down",
                })

    # ── 卖出侧 ──
    if ls.get("current_fen", 0) > 0 and fp is not None:
        # 减仓 50%（浮盈≥100%）—— 口径：点位再涨 X%
        if not ls.get("has_reduced"):
            if fp < 1.00:
                rise = (1.0 - fp) / (1.0 + fp)
                gaps.append({
                    "name": "减仓 50%",
                    "big": f"+{rise*100:.1f}%",
                    "headline": f"点位再涨 {rise*100:.1f}%，触发减仓 50%",
                    "note": "卖出一半，锁定收益",
                    "current": f"当前浮盈 {fp*100:.1f}%，目标 100%",
                    "progress": clamp01(fp / 1.00),
                    "tone": "up",
                })
            else:
                gaps.append({
                    "name": "减仓 50%",
                    "big": "已满足",
                    "headline": "浮盈已达 100%，可执行减仓 50%（卖出一半锁定收益）",
                    "note": "",
                    "current": f"当前浮盈 {fp*100:.1f}%",
                    "progress": 1.0,
                    "tone": "up",
                })

        # 已武装：全部清仓
        if state.get("armed") and ma120 is not None and close:
            from src.engine import count_exit_streak
            streak = count_exit_streak(hist, today_str)
            if close < float(ma120):
                days_left = max(0, 3 - streak)
                gaps.append({
                    "name": "全部清仓",
                    "big": f"{days_left} 天" if days_left > 0 else "已满足",
                    "headline": (f"已跌破 MA120，现状再持续 {days_left} 天触发全部清仓"
                                 if days_left > 0 else "已连续 3 日满足，可全部清仓"),
                    "note": "计日已含 MA120 下行验证，系统自动判断",
                    "current": f"已连续 {streak} 日满足 / 需 3 日",
                    "progress": clamp01(streak / 3.0),
                    "tone": "warn",
                })
            else:
                fall = (float(close) - float(ma120)) / float(close)
                gaps.append({
                    "name": "全部清仓",
                    "big": f"{fall*100:.1f}%",
                    "headline": f"点位再跌 {fall*100:.1f}%（跌破 MA120），准备全部清仓",
                    "note": "跌破后系统自动计日，3 日达标（含 MA120 下行验证）则触发",
                    "current": f"收盘 {close:.0f} / MA120 {ma120:.0f}",
                    "progress": clamp01(1 - fall),
                    "tone": "down",
                })

        # 进入止盈观察期（未进入）—— PB分位≥80% 且 浮盈≥70%
        if not state.get("observation_entered"):
            pb_prog = (pb_pct / 0.80) if pb_pct is not None else 0
            fp_prog = fp / 0.70
            if pb_prog <= fp_prog:
                gap_pp = max(0, (0.80 - (pb_pct or 0)) * 100)
                big = f"+{gap_pp:.0f}%"
                headline = f"PB 分位再升 {gap_pp:.0f}%，进入止盈观察期"
            else:
                gap_pp = max(0, (0.70 - fp) * 100)
                big = f"+{gap_pp:.0f}%"
                headline = f"浮盈再升 {gap_pp:.0f}%，进入止盈观察期"
            gaps.append({
                "name": "止盈观察期",
                "big": big,
                "headline": headline,
                "note": "进入后才武装止盈",
                "current": f"PB 分位 {(pb_pct or 0)*100:.0f}%（需≥80%） · 浮盈 {fp*100:.0f}%（需≥70%）",
                "progress": clamp01(min(pb_prog, fp_prog)),
                "tone": "neutral",
            })

    return gaps
