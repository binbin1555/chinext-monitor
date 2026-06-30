"""
主入口：每日定时运行。
fetch → engine → metrics → notify → dashboard → commit（由 CI 完成）

运行：python src/main.py
     python src/main.py --fetch-only   # 仅更新数据，不发推送
     python src/main.py --report-only  # 仅发日报，不重新拉数据
"""
import os
import sys
import json
import logging
import argparse
import pandas as pd
from pathlib import Path
from datetime import date, datetime, timedelta

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import yaml
from src.fetch    import update_history
from src.engine   import Engine, recalc_from_ledger, calc_warnings
from src.metrics  import build_nav_series, calc_performance, calc_gaps
from src.notify   import BarkNotifier
from src.dashboard import generate_data_json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


def load_config():
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state():
    path = ROOT / "state.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(ROOT / "state.json", "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_ledger():
    path = ROOT / "ledger.csv"
    if path.exists() and path.stat().st_size > 50:
        df = pd.read_csv(path, dtype={"date": str, "action": str,
                                       "fen": int, "price": float, "note": str})
        return df
    return pd.DataFrame(columns=["date", "action", "fen", "price", "note"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch-only",  action="store_true")
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--no-push",     action="store_true")
    args = parser.parse_args()

    config  = load_config()
    state   = load_state()
    ledger  = load_ledger()
    bark_key = os.environ.get("BARK_KEY", "")
    notifier = BarkNotifier(bark_key)
    data_dir  = ROOT / "data"
    docs_dir  = ROOT / "docs"
    today_str = date.today().strftime("%Y-%m-%d")

    logger.info(f"=== 创业板监测系统启动 {today_str} ===")

    # ── Step 1：更新数据 ──
    if not args.report_only:
        try:
            hist = update_history(config, data_dir)
        except Exception as e:
            logger.error(f"理杏仁数据拉取失败: {e}")
            notifier.send_error(
                f"⛔ 数据获取失败（{today_str}）\n"
                f"错误: {e}\n"
                f"策略引擎已暂停，不使用缓存数据。\n"
                f"请检查 LIXINGER_TOKEN 及网络连通性。"
            )
            # 仅用缓存刷新仪表盘展示，不运行引擎
            csv_path = data_dir / "history.csv"
            if csv_path.exists():
                hist_cache = pd.read_csv(csv_path, dtype={"date": str})
                _update_dashboard(hist_cache, state, ledger, config, docs_dir)
            return  # 严禁继续运行策略引擎
    else:
        csv_path = data_dir / "history.csv"
        hist = pd.read_csv(csv_path, dtype={"date": str}) if csv_path.exists() else pd.DataFrame()

    if len(hist) == 0:
        logger.error("历史数据为空，退出")
        return

    # ── Step 2：判断是否为交易日 ──
    latest_date = hist["date"].max()
    if latest_date != today_str:
        logger.info(f"今日（{today_str}）非交易日，最新数据日期 {latest_date}，跳过引擎运行")
        # 仍然更新仪表盘数据（不发推送）
        _update_dashboard(hist, state, ledger, config, docs_dir)
        return

    if args.fetch_only:
        _update_dashboard(hist, state, ledger, config, docs_dir)
        logger.info("--fetch-only 模式，数据已更新")
        return

    # ── Step 2.5：清理已执行的 pending signals ──
    if state.get("signals_pending") and len(ledger) > 0:
        action_map = {
            'T1':'buy','T2':'buy','T3':'buy',
            'weekly_3':'buy','weekly_6':'buy','rightside':'buy',
            'reduce':'reduce','exit':'exit',
        }
        cleaned = []
        for sig in state["signals_pending"]:
            sig_date = sig.get("date", "")
            sig_action = action_map.get(sig.get("type", ""), "buy")
            match = ledger[(ledger["date"] == sig_date) & (ledger["action"] == sig_action)]
            if len(match) == 0:
                cleaned.append(sig)
        if len(cleaned) != len(state["signals_pending"]):
            logger.info(f"已清理 {len(state['signals_pending'])-len(cleaned)} 条已执行信号")
        state["signals_pending"] = cleaned
    # 清除超过 7 天的过期信号
    if state.get("signals_pending"):
        cutoff = (date.today() - timedelta(days=7)).strftime("%Y-%m-%d")
        before = len(state["signals_pending"])
        state["signals_pending"] = [
            s for s in state["signals_pending"] if s.get("date", "") >= cutoff
        ]
        if len(state["signals_pending"]) < before:
            logger.info(f"已清理 {before - len(state['signals_pending'])} 条过期信号（>7天）")

    # ── Step 3：运行策略引擎 ──
    engine  = Engine(config, state, ledger, hist)
    signals = engine.run_daily(today_str)
    state   = engine.state

    warnings = calc_warnings(hist, today_str, state, ledger, config)

    # ── Step 4：推送信号 ──
    if not args.no_push:
        ls = recalc_from_ledger(ledger, config.get("total_fen", 150))
        for sig in signals:
            notifier.send_signal(sig, today_str,
                                  ls.get("total_bought", 0),
                                  config.get("total_fen", 150))
        if warnings:
            notifier.send_warnings(warnings, today_str)

    # ── Step 5：记录 pending signals + 永久触发日志 ──
    state.setdefault("trigger_log", [])
    for sig in signals:
        if sig["type"] not in ("enter_observation",):  # 这类不需要用户操作
            price_val = sig.get("price")
            entry = {
                "date": today_str,
                "type": sig["type"],
                "fen": sig.get("fen", 0),
                "price": round(float(price_val), 2) if price_val is not None else None,
                "reason": sig.get("reason", ""),
            }
            state.setdefault("signals_pending", []).append(entry.copy())
            state["trigger_log"].append(entry)  # 永久保留，不过期

    # ── Step 6：更新仪表盘数据 ──
    _update_dashboard(hist, state, ledger, config, docs_dir, warnings=warnings)

    # ── Step 7：发日报 ──
    if not args.no_push:
        ls    = recalc_from_ledger(ledger, config.get("total_fen", 150))
        nav_df = build_nav_series(hist, ledger, config, state)
        nav_arr = nav_df["nav_portfolio"].tolist() if len(nav_df) > 0 else [1.0]
        perf  = calc_performance(
            nav_arr, config.get("start_date", "2023-01-01"),
            config.get("cash_rate_annual", 0.02)
        )
        today_row = hist[hist["date"] == today_str].iloc[0].to_dict() if len(
            hist[hist["date"] == today_str]) > 0 else {}
        notifier.send_daily_report(
            today_str, perf, state, ls, today_row, warnings
        )

    # ── Step 8：保存状态 ──
    state["last_successful_run"] = today_str
    save_state(state)
    logger.info(f"state.json 已更新，signals={[s['type'] for s in signals]}")


def _update_dashboard(hist, state, ledger, config, docs_dir, warnings=None):
    """更新 docs/data.json（仪表盘数据层）。"""
    try:
        nav_df = build_nav_series(hist, ledger, config, state)
        nav_arr = nav_df["nav_portfolio"].tolist() if len(nav_df) > 0 else [1.0]
        perf   = calc_performance(
            nav_arr,
            config.get("start_date", "2023-01-01"),
            config.get("cash_rate_annual", 0.02),
        )
        today_str = hist["date"].max()
        gaps      = calc_gaps(hist, today_str, state, ledger, config)
        generate_data_json(
            hist=hist, state=state, ledger=ledger, config=config,
            metrics_perf=perf, nav_df=nav_df,
            gaps=gaps, warnings=warnings or [],
            docs_dir=docs_dir,
        )
    except Exception as e:
        logger.error(f"仪表盘数据更新失败: {e}")


if __name__ == "__main__":
    main()
