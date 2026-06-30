"""
历史回放引擎：从历史数据还原每日策略状态，验证第12节金标准。
运行：python src/backtest.py
     python src/backtest.py --reset   # 重算并写入 state.json
"""
import sys
import json
import logging
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.engine import Engine, recalc_from_ledger
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── 金标准（第12节）──
GOLD = {
    1: {
        "T1": ("2018-06-19", 1547),
        "T2": ("2018-08-17", 1434),
        "T3": ("2018-10-16", 1217),
        "avg_approx": 1363,
        "reduce": ("2020-07-09", 2758),
        "exit":   ("2022-01-11", 3056),
    },
    2: {
        "T1": ("2023-04-24", 2301),
        "T2": ("2023-04-25", 2259),
        "T3": ("2023-05-31", 2193),
        "avg_approx": 2234,
        "still_holding_as_of": "2026-06-18",
        "still_holding_price": 4252,
        "float_profit_approx": 0.90,
    },
}


def run_backtest(hist: pd.DataFrame, config: dict,
                 start_date: str = "2016-01-01",
                 end_date: str = None) -> dict:
    """
    从 start_date 到 end_date 逐日回放，返回：
      all_signals: [(date, signal_dict)]
      final_state: dict
      ledger_sim: pd.DataFrame（模拟流水）
    """
    total_fen = int(config.get("total_fen", 150))
    initial_capital = float(config.get("initial_capital", 2_000_000))

    state = {
        "cycle_id": 1,
        "cycle_start_date": None,
        "fen_size": initial_capital / total_fen,
        "t1_fired": False, "t2_fired": False, "t3_fired": False,
        "rightside_used": False,
        "armed": False, "reduced": False,
        "observation_entered": False, "exit_streak": 0,
        "signals_pending": [], "phase": "waiting",
    }

    sub = hist[(hist["date"] >= start_date)]
    if end_date:
        sub = sub[sub["date"] <= end_date]
    sub = sub.sort_values("date").reset_index(drop=True)

    all_signals = []
    cycle_rows = []   # 当前轮工作账本（喂给引擎；清仓后清空，模拟新一轮空账本）
    all_rows   = []   # 全程账本（用于返回与均价验证）

    for _, row in sub.iterrows():
        today = str(row["date"])

        # 为回测自动记账（不模拟用户填写，直接用当日收盘价）
        sim_ledger = pd.DataFrame(cycle_rows,
                                  columns=["date", "action", "fen", "price", "note"])

        engine = Engine(config, state, sim_ledger, sub)
        signals = engine.run_daily(today)
        state = engine.state  # 获取更新后的 state

        for sig in signals:
            stype = sig["type"]
            price = sig.get("price", row["close"])
            fen   = sig.get("fen", 0)

            if stype in ("T1", "T2", "T3", "weekly_6", "weekly_3", "rightside"):
                if fen > 0:
                    rec = {
                        "date": today, "action": "buy",
                        "fen": fen, "price": price,
                        "note": sig.get("reason", stype),
                    }
                    cycle_rows.append(rec); all_rows.append(rec)
            elif stype == "reduce":
                rec = {
                    "date": today, "action": "reduce",
                    "fen": fen, "price": price,
                    "note": sig.get("reason", "减仓50%"),
                }
                cycle_rows.append(rec); all_rows.append(rec)
            elif stype == "exit":
                # 清仓时把所有剩余卖出
                sim_ledger2 = pd.DataFrame(cycle_rows,
                                           columns=["date", "action", "fen", "price", "note"])
                ls = recalc_from_ledger(sim_ledger2, total_fen)
                exit_fen = ls["current_fen"]
                if exit_fen > 0:
                    rec = {
                        "date": today, "action": "exit",
                        "fen": exit_fen, "price": price,
                        "note": sig.get("reason", "全部止盈"),
                    }
                    cycle_rows.append(rec); all_rows.append(rec)
                # 清仓 → 新一轮空账本（实盘中清仓后重设150份）
                cycle_rows = []
                # 重置状态进入下一轮
                state = {
                    "cycle_id": state["cycle_id"] + 1,
                    "cycle_start_date": None,
                    "fen_size": initial_capital / total_fen,
                    "t1_fired": False, "t2_fired": False, "t3_fired": False,
                    "rightside_used": False,
                    "armed": False, "reduced": False,
                    "observation_entered": False, "exit_streak": 0,
                    "signals_pending": [], "phase": "waiting",
                }

            all_signals.append((today, sig))

        if signals:
            logger.debug(f"{today}: {[s['type'] for s in signals]}")

    sim_ledger = pd.DataFrame(
        all_rows, columns=["date", "action", "fen", "price", "note"]
    )
    return {"all_signals": all_signals, "final_state": state, "ledger_sim": sim_ledger}


def validate_gold_standard(all_signals: list, ledger_sim: pd.DataFrame,
                            config: dict) -> bool:
    """对照第12节金标准验收。"""
    total_fen = int(config.get("total_fen", 150))
    passed = True
    print("\n" + "=" * 60)
    print("验收：历史回放金标准对照（第12节）")
    print("=" * 60)

    def check(label, actual_date, expected_date, tolerance_days=3):
        nonlocal passed
        if actual_date is None:
            print(f"  ✗ {label}: 未触发（预期 {expected_date}）")
            passed = False
            return
        actual_dt   = datetime.strptime(actual_date,   "%Y-%m-%d")
        expected_dt = datetime.strptime(expected_date, "%Y-%m-%d")
        diff = abs((actual_dt - expected_dt).days)
        ok = diff <= tolerance_days
        sym = "✓" if ok else "✗"
        print(f"  {sym} {label}: 实际 {actual_date}  预期 {expected_date}  差{diff}天")
        if not ok:
            passed = False

    # 提取信号日期。cycle 以 exit 为界：一轮 = 从开始到 exit（含 exit）。
    def first_signal(sig_types, cycle=None):
        types = sig_types if isinstance(sig_types, (list, tuple)) else [sig_types]
        exits_seen = 0
        for d, s in all_signals:
            this_cycle = exits_seen + 1   # 当前信号所属轮次
            if s["type"] in types and (cycle is None or this_cycle == cycle):
                return d
            if s["type"] == "exit":
                exits_seen += 1
        return None

    # ── 第1轮 ──
    print("\n▶ 第1轮")
    check("T1", first_signal("T1", 1), GOLD[1]["T1"][0])
    check("T2", first_signal("T2", 1), GOLD[1]["T2"][0])
    check("T3", first_signal("T3", 1), GOLD[1]["T3"][0])
    check("减仓", first_signal("reduce", 1), GOLD[1]["reduce"][0])
    check("全清仓", first_signal("exit", 1), GOLD[1]["exit"][0])

    # ── 第2轮 ──
    print("\n▶ 第2轮")
    check("T1", first_signal("T1", 2), GOLD[2]["T1"][0])
    check("T2", first_signal("T2", 2), GOLD[2]["T2"][0])
    check("T3", first_signal("T3", 2), GOLD[2]["T3"][0])

    # 加权均价验证
    rows_c2 = ledger_sim[
        (ledger_sim["date"] >= GOLD[2]["T1"][0]) &
        (ledger_sim["action"] == "buy")
    ]
    if len(rows_c2):
        avg = (rows_c2["fen"] * rows_c2["price"]).sum() / rows_c2["fen"].sum()
        exp = GOLD[2]["avg_approx"]
        diff_pct = abs(avg - exp) / exp
        sym = "✓" if diff_pct < 0.03 else "✗"
        print(f"  {sym} 第2轮均价: {avg:.0f}  预期≈{exp}  差{diff_pct*100:.1f}%")

    print()
    print("结论：" + ("✅ 全部通过" if passed else "❌ 有不符项，请检查规则实现"))
    return passed


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="回放后更新 state.json")
    args = parser.parse_args()

    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    history_csv = ROOT / "data" / "history.csv"
    if not history_csv.exists():
        print(f"未找到 {history_csv}，请先运行 python src/main.py --fetch-only")
        sys.exit(1)

    hist = pd.read_csv(history_csv, dtype={"date": str})
    result = run_backtest(hist, config)

    validate_gold_standard(result["all_signals"], result["ledger_sim"], config)

    if args.reset:
        state_path = ROOT / "state.json"
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(result["final_state"], f, ensure_ascii=False, indent=2)
        print(f"\nstate.json 已更新（回放到 {result['final_state'].get('last_successful_run', '?')}）")
