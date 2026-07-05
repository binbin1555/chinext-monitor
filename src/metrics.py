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
    held_fen   = 0     # 当前持仓份数（用于按"卖出前持仓"比例卖出）
    per_fen    = None  # 当前轮每份金额【复利滚入】：新一轮首笔买入日=当时全部现金÷150
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
            # 复利滚入（手册§三）：持仓归零后的首笔买入=新一轮起点，
            # 用"本金+全部历史收益"（即当时现金）重设 150 份
            if action == "buy" and held_fen == 0:
                per_fen = cash / total_fen
            amount = fen * (per_fen if per_fen else initial_cap / total_fen)

            if action == "buy":
                cash     -= amount
                holdings += amount / price   # 购入的指数份额（点为单位）
                held_fen += fen
            elif action in ("reduce", "exit"):
                # 按"卖出前持仓份数"的比例卖出对应份额（分母必须是卖出前的持仓）
                if held_fen > 0:
                    sell_shares = holdings * (min(fen, held_fen) / held_fen)
                else:
                    sell_shares = 0.0
                cash     += sell_shares * price   # 实际卖出按当时流水价
                holdings -= sell_shares
                held_fen  = max(0, held_fen - fen)

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


def build_theoretical_ledger(state: dict) -> pd.DataFrame:
    """
    从【策略理论账本】构造买卖流水，供净值/总资产按规则计算——与用户是否
    手动记账无关。列：date, action, fen, price。
    包含【已归档的历史轮次】(completed_cycles[*].buys/sells) + 当前轮
    (cycle_buys/cycle_sells)：轮次重置后总资产/净值才能跨轮连续，
    已止盈落袋的利润不会因开新轮而从账上消失。
    """
    rows = []

    def _add_buy(b):
        rows.append({"date": str(b.get("date")), "action": "buy",
                     "fen": int(b.get("fen", 0)), "price": float(b.get("price", 0) or 0)})

    def _add_sell(s):
        rows.append({"date": str(s.get("date")), "action": s.get("action", "reduce"),
                     "fen": int(s.get("fen", 0)), "price": float(s.get("price", 0) or 0)})

    for cyc in (state.get("completed_cycles") or []):
        for b in (cyc.get("buys") or []):
            _add_buy(b)
        for s in (cyc.get("sells") or []):
            _add_sell(s)
    for b in (state.get("cycle_buys") or []):
        _add_buy(b)
    for s in (state.get("cycle_sells") or []):
        _add_sell(s)

    if not rows:
        return pd.DataFrame(columns=["date", "action", "fen", "price"])
    return pd.DataFrame(rows).sort_values("date", kind="stable").reset_index(drop=True)


def calc_asset_totals(hist: pd.DataFrame, events: pd.DataFrame, config: dict) -> dict:
    """
    按规则账本回放到最新交易日，返回总资产口径的金额（含已止盈落袋的现金）：
      total_assets   总资产 = 货基现金 + 持仓市值
      cash           落袋/未投现金（在货基计息）
      position_value 当前持仓市值
      total_pnl      总盈亏 = 总资产 − 本金
      total_return   总收益率
      fen_value      当前每份金额（持仓中=本轮每份；空仓=按当前现金测算的下一轮每份）
    口径与 build_nav_series 完全一致【复利滚入】：每轮首笔买入日，用当时全部现金
    （本金+历史收益）÷ total_fen 重设每份金额（手册§三，2026-07-04 修订）。
    """
    initial_cap = float(config.get("initial_capital", 2_000_000))
    total_fen   = int(config.get("total_fen", 150))
    cash_rate   = float(config.get("cash_rate_annual", 0.02))
    start_date  = config.get("start_date", "2023-01-01")

    sub = hist[hist["date"] >= start_date].reset_index(drop=True)
    if len(sub) == 0:
        return dict(total_assets=initial_cap, cash=initial_cap, position_value=0.0,
                    total_pnl=0.0, total_return=0.0,
                    fen_value=initial_cap / total_fen)

    lbd = {}
    if events is not None and len(events) > 0:
        for _, r in events.iterrows():
            lbd.setdefault(str(r["date"]), []).append(r)

    cash = initial_cap; holdings = 0.0; held = 0; last_close = None
    per_fen = None   # 当前轮每份金额（复利滚入：新一轮首笔买入日以当时现金重设）
    for i in range(len(sub)):
        c = float(sub["close"].iloc[i])
        if np.isnan(c):
            continue
        last_close = c
        for r in lbd.get(str(sub["date"].iloc[i]), []):
            a = str(r["action"]).lower(); fen = int(r["fen"]); pr = float(r["price"])
            if a == "buy" and held == 0:
                per_fen = cash / total_fen   # 复利滚入：本金+全部历史收益等分150份
            amt = fen * (per_fen if per_fen else initial_cap / total_fen)
            if a == "buy":
                cash -= amt; holdings += (amt / pr if pr else 0); held += fen
            elif a in ("reduce", "exit"):
                ss = holdings * (min(fen, held) / held) if held > 0 else 0.0
                cash += ss * pr; holdings -= ss; held = max(0, held - fen)
        cash *= (1 + ((1 + cash_rate) ** (1 / 252) - 1))

    position_value = holdings * (last_close or 0.0)
    total_assets   = cash + position_value
    fen_value = per_fen if (held > 0 and per_fen) else cash / total_fen
    return dict(total_assets=total_assets, cash=cash, position_value=position_value,
                total_pnl=total_assets - initial_cap,
                total_return=total_assets / initial_cap - 1,
                fen_value=fen_value)


def calc_fundamental_sentinel(hist: pd.DataFrame) -> dict:
    """
    基本面哨兵（价值陷阱预警）——监测创业板指的"每点净资产"与"每点盈利"增速。

    原理：低 PB 分位是"真便宜"的前提是分母（净资产 B）没有萎缩。
    B = close / pb，E = close / pe_ttm（均为指数每点口径，只用比率，与规模无关）。
    若 B 连续两年负增长，"便宜"很可能来自分母塌陷——价值陷阱特征成立，
    低 PB 分位失真，买入信号可信度下降，需人工评估择时仓位。

    计算：滚动 250 交易日窗口（≈1年）均值，逐窗口同比：
      b_g1 = 最近一年B均值 / 上一年 − 1      b_g2 = 上一年 / 上上年 − 1
    状态判定（只用 B；盈利 E 波动天然大，仅展示不参与判定）：
      ok     b_g1 ≥ 0                    净资产仍在增长，低PB分位可信
      watch  b_g1 < 0 且 b_g2 ≥ 0        单年负增长（可能是一次性减值），观察
      alert  b_g1 < 0 且 b_g2 < 0        连续两年负增长 → 价值陷阱警报
      na     有效数据不足 500 个交易日     无法评估
    """
    out = {"status": "na", "b_g1": None, "b_g2": None, "e_g1": None,
           "b_yearly": [], "detail": "历史数据不足，暂无法评估"}
    if hist is None or len(hist) == 0:
        return out

    df = hist[["date", "close", "pb", "pe_ttm"]].copy()
    for c in ("close", "pb", "pe_ttm"):
        df[c] = pd.to_numeric(df[c], errors="coerce")

    valid_b = df[(df["close"] > 0) & (df["pb"] > 0)].reset_index(drop=True)
    b = (valid_b["close"] / valid_b["pb"]).values

    def _win_mean(arr, k):
        """倒数第 k 个 250 日窗口均值（k=0 最近）；数据不足返回 None。"""
        end = len(arr) - k * 250
        start = end - 250
        if start < 0:
            return None
        return float(np.mean(arr[start:end]))

    w0, w1, w2 = _win_mean(b, 0), _win_mean(b, 1), _win_mean(b, 2)
    if w0 is not None and w1 is not None:
        out["b_g1"] = w0 / w1 - 1
    if w1 is not None and w2 is not None:
        out["b_g2"] = w1 / w2 - 1

    # 每点盈利（仅展示）：PE 可能为负/极端值，只取正值行
    valid_e = df[(df["close"] > 0) & (df["pe_ttm"] > 0)].reset_index(drop=True)
    e = (valid_e["close"] / valid_e["pe_ttm"]).values
    e0, e1 = _win_mean(e, 0), _win_mean(e, 1)
    if e0 is not None and e1 is not None:
        out["e_g1"] = e0 / e1 - 1

    # 自然年 B 均值同比（页面展示历史脉络；当年需 ≥120 个有效交易日才计入）
    vb = valid_b.copy()
    vb["year"] = vb["date"].str[:4]
    vb["b"] = vb["close"] / vb["pb"]
    yearly = vb.groupby("year")["b"].agg(["mean", "count"])
    yearly = yearly[yearly["count"] >= 120]
    years = yearly.index.tolist()
    for i in range(1, len(years)):
        g = float(yearly["mean"].iloc[i] / yearly["mean"].iloc[i - 1] - 1)
        out["b_yearly"].append({"year": int(years[i]), "growth": round(g, 4)})

    # ── 状态判定 ──
    g1, g2 = out["b_g1"], out["b_g2"]
    if g1 is None:
        return out
    pct = lambda v: f"{v*100:+.1f}%"
    if g1 >= 0:
        out["status"] = "ok"
        out["detail"] = (f"指数每点净资产近一年 {pct(g1)}，基本面仍在增长，"
                         f"低 PB 分位是可信的便宜。")
    elif g2 is None or g2 >= 0:
        out["status"] = "watch"
        out["detail"] = (f"指数每点净资产近一年 {pct(g1)}（转负）。单年负增长可能是"
                         f"商誉减值等一次性因素，暂观察；连续两年才升级为警报。"
                         f"本提示不涉及当前持仓——持仓仍由减仓/清仓规则管理。")
    else:
        out["status"] = "alert"
        out["detail"] = (f"指数每点净资产连续两年负增长（{pct(g2)}、{pct(g1)}）——"
                         f"价值陷阱特征，需人工确诊（核对扣非ROE，见确诊清单）。"
                         f"本警报不涉及当前持仓（持仓去留由减仓/清仓规则决定），"
                         f"只决定下一轮建仓是否放行：确诊为周期性→解除照常执行；"
                         f"确诊为结构性→冻结下一轮新建仓。")
    return out


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
    pe_pct  = row["pe_pct10y"] if not pd.isna(row["pe_pct10y"]) else None
    close   = row["close"]     if not pd.isna(row["close"])     else None
    ma120   = row["ma120"]     if not pd.isna(row["ma120"])     else None
    total_fen = config.get("total_fen", 150)

    # 缺口（距减仓/距止盈）基于【策略理论账本】，与引擎信号时机口径一致
    from src.engine import theoretical_position
    ls = theoretical_position(state, total_fen)
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
        # 减仓 50%（浮盈≥80%，2026-07 由 100% 下调）—— 口径：点位再涨 X%
        # "曾达"语义：本轮浮盈一旦触及 80%（reduce_armed）即视为已满足，与引擎一致
        reduce_qualified = bool(state.get("reduce_armed")) or fp >= 0.80
        if not ls.get("has_reduced"):
            if not reduce_qualified:
                rise = (0.80 - fp) / (1.0 + fp)
                gaps.append({
                    "name": "减仓 50%",
                    "big": f"+{rise*100:.1f}%",
                    "headline": f"点位再涨 {rise*100:.1f}%，触发减仓 50%",
                    "note": "卖出一半，锁定收益",
                    "current": f"当前浮盈 {fp*100:.1f}%，目标 80%",
                    "progress": clamp01(fp / 0.80),
                    "tone": "up",
                })
            else:
                gaps.append({
                    "name": "减仓 50%",
                    "big": "已满足",
                    "headline": "浮盈曾达 80%，请立即减仓 50%（卖出一半锁定收益）",
                    "note": "",
                    "current": f"当前浮盈 {fp*100:.1f}%（曾达80%即触发）",
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

        # 进入止盈观察期（未进入）—— 手册4.1三条路径都建模，取最接近触发的一条展示：
        #   A: 浮盈≥100%（无视估值）  B: PB分位≥80% 且 浮盈≥70%  C: PE分位≥80% 且 浮盈≥80%
        if not state.get("observation_entered"):
            paths = []
            # A：浮盈≥100%
            paths.append({
                "prog": fp / 1.00,
                "big": f"+{max(0, (1.00 - fp)) * 100:.0f}%",
                "headline": f"浮盈再升 {max(0, (1.00 - fp)) * 100:.0f}%（达100%，无视估值），进入止盈观察期",
                "current": f"浮盈 {fp*100:.0f}%（需≥100%）",
            })
            # B：PB≥80% 且 浮盈≥70%
            if pb_pct is not None:
                pb_prog, fp_prog = pb_pct / 0.80, fp / 0.70
                if pb_prog <= fp_prog:
                    h = f"PB 分位再升 {max(0, (0.80 - pb_pct)) * 100:.0f}%，进入止盈观察期"
                    b = f"+{max(0, (0.80 - pb_pct)) * 100:.0f}%"
                else:
                    h = f"浮盈再升 {max(0, (0.70 - fp)) * 100:.0f}%，进入止盈观察期"
                    b = f"+{max(0, (0.70 - fp)) * 100:.0f}%"
                paths.append({
                    "prog": min(pb_prog, fp_prog), "big": b, "headline": h,
                    "current": f"PB 分位 {pb_pct*100:.0f}%（需≥80%） · 浮盈 {fp*100:.0f}%（需≥70%）",
                })
            # C：PE≥80% 且 浮盈≥80%
            if pe_pct is not None:
                pe_prog, fp_prog = pe_pct / 0.80, fp / 0.80
                if pe_prog <= fp_prog:
                    h = f"PE 分位再升 {max(0, (0.80 - pe_pct)) * 100:.0f}%，进入止盈观察期"
                    b = f"+{max(0, (0.80 - pe_pct)) * 100:.0f}%"
                else:
                    h = f"浮盈再升 {max(0, (0.80 - fp)) * 100:.0f}%，进入止盈观察期"
                    b = f"+{max(0, (0.80 - fp)) * 100:.0f}%"
                paths.append({
                    "prog": min(pe_prog, fp_prog), "big": b, "headline": h,
                    "current": f"PE 分位 {pe_pct*100:.0f}%（需≥80%） · 浮盈 {fp*100:.0f}%（需≥80%）",
                })
            best = max(paths, key=lambda p: p["prog"])
            gaps.append({
                "name": "止盈观察期",
                "big": best["big"],
                "headline": best["headline"],
                "note": "三条路径满足任一即进入；进入后才武装止盈",
                "current": best["current"],
                "progress": clamp01(best["prog"]),
                "tone": "neutral",
            })

    return gaps
