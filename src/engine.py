"""
策略规则引擎：严格按操作手册第4节实现，不增删规则。

输入：history DataFrame（含今日最新行）、state dict、ledger DataFrame、config dict
输出：signals 列表（今日触发的买/卖信号）、更新后的 state
"""
import json
import logging
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 牛市段规则（手册 4.3，2026-11 修订）：武装后用年线管理仓位
#   收盘 < 年线 × (1 − 3%) → 卖出全部剩余创业板
#   卖出后收盘 > 年线 × (1 + 3%) → 买回到卖出前的仓位
#   卖出、买回循环，直到下一轮开启（武装后 PB 分位回到 ≥20%，空仓时再跌破 20%）
# ─────────────────────────────────────────────
TREND_MA_LEN = 250      # 年线：最近 250 个交易日收盘均值（含当日）
TREND_BAND   = 0.03     # 卖出线 = 年线 × 0.97；买回线 = 年线 × 1.03
NEW_CYCLE_PB = 0.20     # 新一轮开启线（与 T1 同一阈值）


def ensure_ma250(hist: pd.DataFrame) -> pd.DataFrame:
    """确保 hist 有年线列 ma250（按收盘价滚动 250 日均值，含当日；不足 250 日为空）。
    原地添加并返回同一个 DataFrame。history.csv 不存这一列，每次运行现算。"""
    if hist is None or len(hist) == 0 or "close" not in hist.columns:
        return hist
    if "ma250" not in hist.columns or hist["ma250"].isna().all():
        closes = pd.to_numeric(hist["close"], errors="coerce")
        hist["ma250"] = closes.rolling(TREND_MA_LEN, min_periods=TREND_MA_LEN).mean()
    return hist


def trend_lines(ma250):
    """返回 (卖出线, 买回线)；年线缺失时返回 (None, None)。"""
    if ma250 is None or pd.isna(ma250):
        return None, None
    return float(ma250) * (1 - TREND_BAND), float(ma250) * (1 + TREND_BAND)


def start_new_cycle(state: dict, start_date: str, end_date: str, avg=None) -> int:
    """归档当前轮的策略理论账本，并把 state 重置为新一轮。返回新一轮的编号。
    归档后总资产/净值仍能跨轮回放（build_theoretical_ledger 会拼接归档轮）。"""
    old_cycle = state.get("cycle_id", 1)
    state.setdefault("completed_cycles", []).append(
        {"cycle_id": old_cycle, "start": state.get("cycle_start_date"), "end": end_date,
         "avg": round(avg, 2) if avg else None,
         "buys":   list(state.get("cycle_buys") or []),
         "sells":  list(state.get("cycle_sells") or []),
         "rebuys": list(state.get("cycle_rebuys") or [])})
    state.update({
        "cycle_id": old_cycle + 1,
        "cycle_start_date": start_date,
        # 复利滚入：新一轮每份金额=建仓首笔买入日的全部资金÷150，事前未知
        "fen_size": None,
        "t1_fired": False, "t2_fired": False, "t3_fired": False,
        "rightside_used": False, "armed": False, "timeout_armed": False,
        "reduced": False, "reduce_armed": False,
        "exited": False, "observation_entered": False, "exit_streak": 0,
        "cycle_buys": [], "cycle_sold_fen": 0, "cycle_sells": [], "cycle_rebuys": [],
        "trend_out": False, "trend_out_fen": 0, "trend_sell_date": None,
        "trend_sell_price": None, "pb_reset_seen": False,
        "last_weekly_week": None, "phase": "waiting",
    })
    return old_cycle + 1

# ─────────────────────────────────────────────
# 流水重算
# ─────────────────────────────────────────────

def recalc_from_ledger(ledger: pd.DataFrame, total_fen: int = 150,
                       cycle_start_date: str = None):
    """
    从 ledger.csv 重算持仓状态。
    cycle_start_date 非空时，只统计该日期（含）之后的流水——确保多轮次下
    每一轮的均价/持仓相互独立，不被上一轮已清仓的流水污染。
    返回：
      total_bought   已累计买入份数
      total_sold     已累计卖出份数（reduce + exit）
      current_fen    当前持仓份数
      weighted_avg   买入加权均价（按总买入份数，不随卖出调整）
      has_reduced    是否已做过 50% 减仓
      has_exited     是否已全部清仓（本轮）
    """
    if ledger is not None and len(ledger) > 0 and cycle_start_date:
        ledger = ledger[ledger["date"] >= cycle_start_date]

    if ledger is None or len(ledger) == 0:
        return dict(
            total_bought=0, total_sold=0, current_fen=0,
            weighted_avg=None, has_reduced=False, has_exited=False,
        )

    buy_rows    = ledger[ledger["action"] == "buy"]
    reduce_rows = ledger[ledger["action"] == "reduce"]
    exit_rows   = ledger[ledger["action"] == "exit"]

    total_bought = int(buy_rows["fen"].sum())
    total_sold   = int(reduce_rows["fen"].sum() + exit_rows["fen"].sum())
    current_fen  = total_bought - total_sold

    if total_bought > 0:
        weighted_avg = float(
            (buy_rows["fen"] * buy_rows["price"]).sum() / total_bought
        )
    else:
        weighted_avg = None

    return dict(
        total_bought=total_bought,
        total_sold=total_sold,
        current_fen=current_fen,
        weighted_avg=weighted_avg,
        has_reduced=len(reduce_rows) > 0,
        has_exited=len(exit_rows) > 0,
    )


def theoretical_position(state: dict, total_fen: int = 150) -> dict:
    """
    引擎自维护的【策略理论账本】——完全由触发信号（按触发日收盘价）构成，
    不读用户 ledger.csv。买卖信号的时机（含减仓/止盈）一律基于它，
    因此【不受用户忘记记账、漏记、或临场微调买卖的影响】。
    结构与 recalc_from_ledger 对齐，供引擎/缺口/预警的决策使用。
    ledger.csv 仅用于在看板显示用户的实际盈亏，绝不参与信号判断。
    """
    buys   = state.get("cycle_buys", []) or []
    rebuys = state.get("cycle_rebuys", []) or []
    bought = sum(int(b["fen"]) for b in buys)
    rebought = sum(int(b["fen"]) for b in rebuys)
    sold   = int(state.get("cycle_sold_fen", 0) or 0)
    # 成本只按本轮 PB 阶梯的买入计算；年线买回不改变成本（+35% 武装、+80% 减半都以它为准）
    avg    = (sum(int(b["fen"]) * float(b["price"]) for b in buys) / bought
              if bought > 0 else None)
    return dict(
        total_bought=bought,          # 阶梯已买份数（决定还剩多少份可买）
        total_rebought=rebought,      # 年线买回份数
        total_sold=sold,              # 已卖份数（减半 + 年线卖出）
        current_fen=max(0, bought + rebought - sold),
        weighted_avg=avg,
        has_reduced=bool(state.get("reduced")),
        has_exited=bool(state.get("exited")),
        trend_out=bool(state.get("trend_out")),
    )


# ─────────────────────────────────────────────
# 辅助：交易日判断
# ─────────────────────────────────────────────

def is_first_trading_day_of_week(today_str: str, hist_dates: list) -> bool:
    """
    判断 today_str 是否是该自然周（ISO 周）的第一个交易日。
    通过检查 hist_dates 中同周有无更早的日期来判断。
    """
    try:
        today_dt = datetime.strptime(today_str, "%Y-%m-%d")
    except Exception:
        return False
    iso_week = today_dt.isocalendar()[:2]  # (year, week)

    for d in hist_dates:
        if d >= today_str:
            continue
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
        except Exception:
            continue
        if dt.isocalendar()[:2] == iso_week:
            return False  # 同周内有更早的交易日
    return True


def get_pb_pct_in_last_n_days(hist: pd.DataFrame, today_str: str, n: int = 120) -> float:
    """返回过去 n 个交易日内 PB 分位的最小值（用于右侧补仓判断）。"""
    past = hist[hist["date"] < today_str].tail(n)
    if len(past) == 0:
        return np.nan
    return float(past["pb_pct10y"].min())


def check_consecutive_above_ma120(hist: pd.DataFrame, today_str: str, n: int = 3) -> bool:
    """检查包括 today 在内，最近 n 个交易日收盘均在 MA120 之上。"""
    sub = hist[hist["date"] <= today_str].tail(n)
    if len(sub) < n:
        return False
    return bool((sub["close"] > sub["ma120"]).all())


def check_ma120_rising(hist: pd.DataFrame, today_str: str, lookback: int = 20) -> bool:
    """当日 MA120 > lookback 个交易日前的 MA120（MA120 上行）。"""
    sub = hist[hist["date"] <= today_str]
    if len(sub) < lookback + 1:
        return False
    ma_now   = sub["ma120"].iloc[-1]
    ma_past  = sub["ma120"].iloc[-(lookback + 1)]
    if pd.isna(ma_now) or pd.isna(ma_past):
        return False
    return bool(ma_now > ma_past)


def check_consecutive_exit_condition(hist: pd.DataFrame, today_str: str, n: int = 3) -> bool:
    """
    检查最近 n 个交易日是否连续满足全清仓条件：
    收盘 < MA120 AND MA120 < 20日前 MA120。
    """
    sub = hist[hist["date"] <= today_str].tail(n + 20)
    dates = sub["date"].tolist()
    if len(dates) < n:
        return False

    last_n_dates = dates[-(n):]
    for d in last_n_dates:
        row = sub[sub["date"] == d]
        if len(row) == 0:
            return False
        close  = row["close"].iloc[0]
        ma120  = row["ma120"].iloc[0]
        if pd.isna(close) or pd.isna(ma120):
            return False
        if close >= ma120:
            return False
        # MA120 方向
        idx = list(sub["date"]).index(d)
        if idx < 20:
            return False
        ma120_past = sub["ma120"].iloc[idx - 20]
        if pd.isna(ma120_past) or ma120 >= ma120_past:
            return False
    return True


def count_exit_streak(hist: pd.DataFrame, today_str: str) -> int:
    """统计截至今日连续满足（收盘<MA120 且 MA120下行）的交易日数。"""
    sub = hist[hist["date"] <= today_str].tail(30 + 20)
    dates = list(sub["date"])
    streak = 0
    for d in reversed(dates):
        row = sub[sub["date"] == d]
        if len(row) == 0:
            break
        close = row["close"].iloc[0]
        ma120 = row["ma120"].iloc[0]
        if pd.isna(close) or pd.isna(ma120) or close >= ma120:
            break
        idx = list(sub["date"]).index(d)
        if idx < 20:
            break
        ma120_past = sub["ma120"].iloc[idx - 20]
        if pd.isna(ma120_past) or ma120 >= ma120_past:
            break
        streak += 1
    return streak


# ─────────────────────────────────────────────
# 主引擎
# ─────────────────────────────────────────────

class Engine:
    def __init__(self, config: dict, state: dict, ledger: pd.DataFrame, hist: pd.DataFrame):
        self.config  = config
        self.state   = state.copy()
        self.ledger  = ledger
        self.hist    = hist
        self.total_fen = int(config.get("total_fen", 150))

    def run_daily(self, today_str: str) -> list:
        """
        对 today_str 这一天运行策略引擎，返回 signals 列表并更新 self.state。
        每个 signal 是一个 dict：{type, fen, price, reason, level}。
        """
        row = self.hist[self.hist["date"] == today_str]
        if len(row) == 0:
            logger.warning(f"{today_str} 在历史数据中不存在，跳过")
            return []

        row = row.iloc[0]
        close   = float(row["close"])   if not pd.isna(row["close"])   else None
        pb_pct  = float(row["pb_pct10y"]) if not pd.isna(row["pb_pct10y"]) else None
        pe_pct  = float(row["pe_pct10y"]) if not pd.isna(row["pe_pct10y"]) else None
        ma120   = float(row["ma120"])   if not pd.isna(row["ma120"])   else None
        ma250   = self._ma250(today_str)

        if close is None or pb_pct is None:
            logger.warning(f"{today_str} 数据不完整（close={close}, pb_pct={pb_pct}），跳过")
            return []

        # 决策全部基于【策略理论账本】（cycle_buys / cycle_sold_fen / cycle_rebuys），不读用户 ledger，
        # 故减仓/卖出/买回时机不受用户手动记账、漏记、临场微调的影响。
        self.state.setdefault("cycle_buys", [])
        self.state.setdefault("cycle_sold_fen", 0)
        self.state.setdefault("cycle_rebuys", [])
        pos = theoretical_position(self.state, self.total_fen)

        signals = []
        hist_dates = list(self.hist[self.hist["date"] <= today_str]["date"])

        # ── 旧规则遗留（2026-11 之前的 MA120 清仓）：已全清仓，等 main.py 重置为新一轮 ──
        if self.state.get("exited") and pos["current_fen"] == 0:
            return []

        # ── 新一轮（手册 4.4）：本轮已武装，且武装后 PB 分位回到过 ≥20%；
        #    已按年线卖出、手里没有创业板时，PB 分位再跌破 20% → 本轮结束，当天开启新一轮 ──
        if self.state.get("armed"):
            if pb_pct >= NEW_CYCLE_PB:
                self.state["pb_reset_seen"] = True
            elif self.state.get("pb_reset_seen") and pos["current_fen"] == 0:
                old_id = self.state.get("cycle_id", 1)
                new_id = start_new_cycle(self.state, today_str, today_str, pos["weighted_avg"])
                signals.append(dict(
                    type="new_cycle", fen=0, price=close, pb_pct=pb_pct,
                    reason=(f"第{old_id}轮结束（已按年线卖出空仓），PB分位重新跌破20%，"
                            f"开启第{new_id}轮建仓：每份 = 当前全部资金 ÷ {self.total_fen}"),
                    level="timeSensitive",
                ))
                pos = theoretical_position(self.state, self.total_fen)

        total_bought  = pos["total_bought"]
        current_fen   = pos["current_fen"]
        weighted_avg  = pos["weighted_avg"]
        remaining_fen = self.total_fen - total_bought

        float_profit  = (close / weighted_avg - 1.0) if weighted_avg else None

        # ══════════════════════════════════════════
        # 买入规则（第 3 节 PB 阶梯）——年线卖出后等待买回期间不加仓，由年线规则决定买回
        # ══════════════════════════════════════════
        if remaining_fen > 0 and not self.state.get("trend_out"):

            # 1. 周定投（每周第一个交易日，每周仅一次——防同日/同周重复触发）
            iso = datetime.strptime(today_str, "%Y-%m-%d").isocalendar()
            week_key = f"{iso[0]}-W{iso[1]:02d}"
            if (is_first_trading_day_of_week(today_str, hist_dates[:-1])
                    and self.state.get("last_weekly_week") != week_key):
                self.state["last_weekly_week"] = week_key
                if pb_pct < 0.15:
                    qty = min(6, remaining_fen)
                    signals.append(_buy_signal("weekly_6", qty, close, pb_pct,
                                               "周定投 PB分位<15% 买6份", "timeSensitive"))
                    remaining_fen -= qty
                elif pb_pct < 0.20:
                    qty = min(3, remaining_fen)
                    signals.append(_buy_signal("weekly_3", qty, close, pb_pct,
                                               "周定投 PB分位15~20% 买3份", "active"))
                    remaining_fen -= qty

            # 2. T1：PB分位首次跌破 20%（金字塔首档，小试）
            if not self.state.get("t1_fired") and pb_pct < 0.20:
                qty = min(10, remaining_fen)
                signals.append(_buy_signal("T1", qty, close, pb_pct,
                                           "T1 PB分位首次<20% 一次性买10份", "timeSensitive"))
                self.state["t1_fired"] = True
                remaining_fen -= qty  # 本日内的 T 档叠加

            # 3. T2：PB分位首次跌破 15%（金字塔加码，越跌越买）
            if not self.state.get("t2_fired") and pb_pct < 0.15:
                qty = min(30, remaining_fen)
                signals.append(_buy_signal("T2", qty, close, pb_pct,
                                           "T2 PB分位首次<15% 一次性买30份", "timeSensitive"))
                self.state["t2_fired"] = True
                remaining_fen -= qty

            # 4. T3：PB分位首次跌破 10%，满仓剩余
            if not self.state.get("t3_fired") and pb_pct < 0.10:
                qty = remaining_fen  # 全部剩余
                if qty > 0:
                    signals.append(_buy_signal("T3", qty, close, pb_pct,
                                               f"T3 PB分位首次<10% 满仓剩余{qty}份", "timeSensitive"))
                self.state["t3_fired"] = True
                remaining_fen = 0

            # 5. 右侧补仓（防踏空，每轮一次）
            if (not self.state.get("rightside_used")
                    and remaining_fen >= 20
                    and ma120 is not None):
                min_pb_120 = get_pb_pct_in_last_n_days(self.hist, today_str, 120)
                if (not pd.isna(min_pb_120) and min_pb_120 < 0.10
                        and check_consecutive_above_ma120(self.hist, today_str, 3)
                        and check_ma120_rising(self.hist, today_str, 20)):
                    qty = min(30, remaining_fen)
                    signals.append(_buy_signal("rightside", qty, close, pb_pct,
                                               "右侧补仓：MA120上行且连续3日站上MA120", "timeSensitive"))
                    self.state["rightside_used"] = True

        # 把本日触发的买入按当日收盘价记入【策略理论账本】。本日卖出评估仍用日初持仓
        # （current_fen/weighted_avg/float_profit 为日初快照），与"信号发出、账本次日更新"一致。
        for _s in signals:
            if _s["type"] in ("weekly_6", "weekly_3", "T1", "T2", "T3", "rightside"):
                self.state["cycle_buys"].append(
                    {"date": today_str, "fen": int(_s["fen"]), "price": float(close)})

        # ══════════════════════════════════════════
        # 卖出 / 买回规则（第 4 节）——有持仓、或年线卖出后等待买回时评估
        # ══════════════════════════════════════════
        in_cycle = current_fen > 0 or bool(self.state.get("trend_out"))
        if in_cycle and weighted_avg is not None and float_profit is not None:

            # 武装检查（浮盈曾达 35%，永久武装）
            if not self.state.get("armed") and float_profit >= 0.35:
                self.state["armed"] = True
                logger.info(f"{today_str} 已武装（浮盈{float_profit:.1%}）")

            # 武装超时（手册4.3.1，休眠保险）：首笔买入起满 N 年仍未武装 →
            # 视同武装。只解锁年线卖出/买回规则，本身不卖出；历史两轮武装用时
            # 1.54/2.39 年，N=4 在 2016–2026 全样本零触发。
            if not self.state.get("armed"):
                buys = self.state.get("cycle_buys") or []
                timeout_y = float(self.config.get("arming_timeout_years", 4))
                if buys and timeout_y > 0:
                    held_days = (datetime.strptime(today_str, "%Y-%m-%d")
                                 - datetime.strptime(buys[0]["date"], "%Y-%m-%d")).days
                    if held_days >= timeout_y * 365:
                        self.state["armed"] = True
                        self.state["timeout_armed"] = True
                        logger.info(f"{today_str} 建仓{held_days/365:.2f}年未武装，"
                                    f"超时视同武装（手册4.3.1）")

            # 武装当天（或之前）PB 分位已 ≥20%：记下，供"新一轮"判断
            if self.state.get("armed") and pb_pct >= NEW_CYCLE_PB:
                self.state["pb_reset_seen"] = True

            # 进入止盈观察期（只提示，不触发买卖）
            if current_fen > 0 and not self.state.get("observation_entered"):
                cond1 = (pe_pct is not None and pe_pct >= 0.80 and float_profit >= 0.80)
                cond2 = float_profit >= 1.00
                cond3 = (pb_pct >= 0.80 and float_profit >= 0.70)
                if cond1 or cond2 or cond3:
                    self.state["observation_entered"] = True
                    reason = ("PE分位≥80%且浮盈≥80%" if cond1 else
                              "浮盈≥100%" if cond2 else "PB分位≥80%且浮盈≥70%")
                    signals.append(dict(
                        type="enter_observation",
                        fen=0, price=close, pb_pct=pb_pct,
                        reason=f"进入止盈观察期（{reason}）",
                        level="timeSensitive",
                    ))

            # 减仓 50%（一次性）——阈值 +80%（2026-07 由 +100% 下调；理由见操作手册4.2）。
            # 采用"曾达"语义（与武装同款）：本轮浮盈一旦触及 80% 即永久满足条件，
            # 防止行情快速跌穿 80% 时漏掉减仓——那正是高位回撤最该被保护的情形。
            if float_profit >= 0.80:
                self.state["reduce_armed"] = True
            if not self.state.get("reduced") and self.state.get("reduce_armed"):
                # 用含本日买入的最新持仓算"一半"（与年线卖出同口径），避免同日
                # 恰有买入信号时按日初快照少卖（理论上仅右侧补仓可能与减仓同日）
                qty = theoretical_position(self.state, self.total_fen)["current_fen"] // 2
                if qty > 0:
                    signals.append(dict(
                        type="reduce",
                        fen=qty, price=close, pb_pct=pb_pct,
                        reason=f"浮盈曾达80%（当前{float_profit:.1%}），卖出一半({qty}份)",
                        level="timeSensitive",
                    ))
                    self.state["reduced"] = True
                    self.state["cycle_sold_fen"] = self.state.get("cycle_sold_fen", 0) + qty
                    # 带价格记入理论账本，供"总资产/落袋现金/净值"按规则计算（不依赖用户记账）
                    self.state.setdefault("cycle_sells", []).append(
                        {"date": today_str, "fen": qty, "price": close, "action": "reduce"})
                elif self.state.get("trend_out"):
                    # 年线卖出后、买回前才第一次到 +80%（极少见）：不用卖，买回时只买回一半
                    self.state["reduced"] = True
                    self.state["trend_out_fen"] = int(self.state.get("trend_out_fen", 0) or 0) // 2

            # 年线卖出 / 买回（武装后，手册 4.3）
            sell_line, buy_line = trend_lines(ma250)
            if self.state.get("armed") and sell_line is not None:
                if not self.state.get("trend_out"):
                    # 重新读取持仓（若同日已减仓，日初快照已过时）
                    out_fen = theoretical_position(self.state, self.total_fen)["current_fen"]
                    if out_fen > 0 and close < sell_line:
                        signals.append(dict(
                            type="trend_exit",
                            fen=out_fen, price=close, pb_pct=pb_pct,
                            reason=(f"已武装，收盘 {close:.0f} 跌破年线卖出线 {sell_line:.0f}"
                                    f"（年线 {ma250:.0f} × 0.97），卖出全部剩余 {out_fen} 份；"
                                    f"之后收盘站上年线 × 1.03 再买回"),
                            level="timeSensitive",
                        ))
                        self.state["cycle_sold_fen"] = self.state.get("cycle_sold_fen", 0) + out_fen
                        self.state.setdefault("cycle_sells", []).append(
                            {"date": today_str, "fen": out_fen, "price": close, "action": "trend_exit"})
                        self.state["trend_out"]        = True
                        self.state["trend_out_fen"]    = out_fen
                        self.state["trend_sell_date"]  = today_str
                        self.state["trend_sell_price"] = round(close, 2)
                elif close > buy_line:
                    back_fen = int(self.state.get("trend_out_fen", 0) or 0)
                    if back_fen > 0:
                        signals.append(dict(
                            type="rebuy",
                            fen=back_fen, price=close, pb_pct=pb_pct,
                            reason=(f"收盘 {close:.0f} 站上年线买回线 {buy_line:.0f}"
                                    f"（年线 {ma250:.0f} × 1.03），买回 {back_fen} 份"
                                    f"（{'减过半，买回到总资产的一半' if self.state.get('reduced') else '买回到卖出前的仓位'}）"),
                            level="timeSensitive",
                        ))
                        self.state["cycle_rebuys"].append(
                            {"date": today_str, "fen": back_fen, "price": close})
                    self.state["trend_out"]     = False
                    self.state["trend_out_fen"] = 0

        # ── 更新阶段标签（用含本日买入的最新理论持仓）──
        pos2 = theoretical_position(self.state, self.total_fen)
        if self.state.get("trend_out"):
            self.state["phase"] = "trend_out"
        elif pos2["current_fen"] > 0:
            self.state["phase"] = "holding"
        else:
            self.state["phase"] = "waiting"

        self.state["last_successful_run"] = today_str
        return signals

    def _ma250(self, today_str: str):
        """当日年线（MA250，含当日）；数据不足 250 日时为 None。"""
        if "ma250" not in self.hist.columns:
            ensure_ma250(self.hist)
        v = self.hist.loc[self.hist["date"] == today_str, "ma250"]
        if len(v) == 0 or pd.isna(v.iloc[0]):
            return None
        return float(v.iloc[0])


def _buy_signal(sig_type, fen, price, pb_pct, reason, level):
    return dict(type=sig_type, fen=fen, price=price, pb_pct=pb_pct,
                reason=reason, level=level)


def bootstrap_cycle_from_history(hist: pd.DataFrame, config: dict,
                                 cycle_start_date: str) -> dict:
    """
    从 cycle_start_date 起【确定性重放】策略，重建本轮的策略理论账本(cycle_buys /
    cycle_sold_fen)与全部状态标志。用途：把旧 state（无理论账本）一次性迁移过来，
    完全不依赖用户 ledger。重放到本轮清仓点（若历史中已清仓）或最新数据为止。
    """
    total_fen = int(config.get("total_fen", 150))
    state = {
        "cycle_id": 1, "cycle_start_date": cycle_start_date,
        "t1_fired": False, "t2_fired": False, "t3_fired": False,
        "rightside_used": False, "armed": False, "reduced": False,
        "reduce_armed": False,
        "exited": False, "observation_entered": False, "exit_streak": 0,
        "cycle_buys": [], "cycle_sold_fen": 0, "cycle_sells": [],
        "last_weekly_week": None, "phase": "waiting",
    }
    empty = pd.DataFrame(columns=["date", "action", "fen", "price", "note"])
    sub = hist[hist["date"] >= cycle_start_date].sort_values("date")
    for d in sub["date"].tolist():
        eng = Engine(config, state, empty, hist)
        eng.run_daily(str(d))
        state = eng.state
        if state.get("exited"):
            break
    return state


# ─────────────────────────────────────────────
# 预警（临近触发）
# ─────────────────────────────────────────────

def calc_warnings(hist: pd.DataFrame, today_str: str, state: dict,
                  ledger: pd.DataFrame, config: dict) -> list:
    """
    计算临近预警（不改变状态），返回 warning 列表。
    """
    warnings = []
    row = hist[hist["date"] == today_str]
    if len(row) == 0:
        return warnings
    row = row.iloc[0]
    pb_pct = float(row["pb_pct10y"]) if not pd.isna(row["pb_pct10y"]) else None
    pe_pct = float(row["pe_pct10y"]) if not pd.isna(row["pe_pct10y"]) else None
    close  = float(row["close"])     if not pd.isna(row["close"])     else None
    ensure_ma250(hist)
    _m250  = hist.loc[hist["date"] == today_str, "ma250"]
    ma250  = float(_m250.iloc[0]) if len(_m250) and not pd.isna(_m250.iloc[0]) else None
    sell_line, buy_line = trend_lines(ma250)

    band = config.get("warn_band_pp", 2) / 100.0
    # 减仓/卖出/买回类预警基于【策略理论账本】，与信号时机口径一致
    ls   = theoretical_position(state, config.get("total_fen", 150))
    fp   = (close / ls["weighted_avg"] - 1) if ls["weighted_avg"] and close else None
    holding   = ls["current_fen"] > 0
    trend_out = bool(state.get("trend_out"))

    # 买入阶梯（本轮还在建仓、不在年线卖出后的等待期）
    if pb_pct is not None and not trend_out:
        if not state.get("t1_fired") and 0.20 <= pb_pct <= 0.20 + band:
            warnings.append(f"⚠️ PB分位{pb_pct:.1%}，距T1触发(<20%)还差{(pb_pct-0.20)*100:.1f}%")
        if not state.get("t2_fired") and 0.15 <= pb_pct <= 0.15 + band:
            warnings.append(f"⚠️ PB分位{pb_pct:.1%}，距T2触发(<15%)还差{(pb_pct-0.15)*100:.1f}%")
        if not state.get("t3_fired") and 0.10 <= pb_pct <= 0.10 + band:
            warnings.append(f"⚠️ PB分位{pb_pct:.1%}，距T3触发(<10%)还差{(pb_pct-0.10)*100:.1f}%")

    if fp is not None and holding:
        if not state.get("armed") and 0.30 <= fp < 0.35:
            warnings.append(f"⚠️ 浮盈{fp:.1%}，距武装(35%)还差{(0.35-fp)*100:.1f}%"
                            f"；武装后启用年线卖出规则")
        if not ls["has_reduced"] and 0.75 <= fp < 0.80:
            warnings.append(f"⚠️ 浮盈{fp:.1%}，距减仓线(80%)还差{(0.80-fp)*100:.1f}%")

    # 观察期一经进入永不取消（手册4.1），进入后不再提示"临近"——
    # 否则分位从高位回落穿过 78~80% 区间时会误报（与 T 档预警查 t*_fired 同理）
    if not state.get("observation_entered"):
        if pe_pct is not None and 0.78 <= pe_pct < 0.80:
            warnings.append(f"⚠️ PE分位{pe_pct:.1%}，临近止盈观察期条件（80%）")
        if pb_pct is not None and 0.78 <= pb_pct < 0.80:
            warnings.append(f"⚠️ PB分位{pb_pct:.1%}，临近止盈观察期条件（80%）")

    # 年线卖出 / 买回预警（武装后）
    if state.get("armed") and close and sell_line is not None:
        if holding and not trend_out:
            gap = (close - sell_line) / close
            if 0 <= gap <= band:
                warnings.append(f"⚠️ 已武装：收盘距年线卖出线（{sell_line:.0f}）只差{gap*100:.1f}%，"
                                f"跌破将卖出全部剩余创业板")
        if trend_out:
            gap = (buy_line - close) / close
            if 0 <= gap <= band:
                warnings.append(f"⚠️ 收盘距年线买回线（{buy_line:.0f}）只差{gap*100:.1f}%，"
                                f"站上将买回{int(state.get('trend_out_fen', 0) or 0)}份")
            if (state.get("pb_reset_seen") and pb_pct is not None
                    and NEW_CYCLE_PB <= pb_pct <= NEW_CYCLE_PB + band):
                warnings.append(f"⚠️ PB分位{pb_pct:.1%}，距开启下一轮(<20%)还差"
                                f"{(pb_pct-NEW_CYCLE_PB)*100:.1f}%")

    return warnings
