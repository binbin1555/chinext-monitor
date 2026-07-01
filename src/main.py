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

# Windows 控制台默认 GBK，日志中的 emoji 会 UnicodeEncodeError——强制 UTF-8 输出
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

import yaml
from src.fetch    import update_history
from src.engine   import (Engine, recalc_from_ledger, calc_warnings,
                           theoretical_position, bootstrap_cycle_from_history)
from src.metrics  import build_nav_series, calc_performance, calc_gaps
from src.notify   import BarkNotifier
from src.dashboard import generate_data_json, load_acknowledged_keys, filter_pending_signals

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
        logger.info(f"今日（{today_str}）最新数据为 {latest_date}，非新交易日，跳过引擎运行")
        # 工作日却没有今日数据：可能是节假日，也可能是数据延迟/定时器过早——提示一次（每日去重）
        is_weekday = datetime.strptime(today_str, "%Y-%m-%d").weekday() < 5
        if (is_weekday and not args.no_push and not args.report_only
                and state.get("last_stale_alert") != today_str):
            notifier.send_info(
                f"ℹ️ 今日暂无新交易数据（{today_str}）",
                f"最新数据仍为 {latest_date}。\n"
                f"若为节假日属正常；若为交易日，请检查理杏仁是否已发布数据、"
                f"或定时器是否在收盘(07:00 UTC)之后触发。"
            )
            state["last_stale_alert"] = today_str
            save_state(state)
        # 仍然更新仪表盘数据（不重复推送）
        _update_dashboard(hist, state, ledger, config, docs_dir)
        return

    if args.fetch_only:
        _update_dashboard(hist, state, ledger, config, docs_dir)
        logger.info("--fetch-only 模式，数据已更新")
        return

    # ── Step 2.6：今日官方 PB 分位缺失 → 告警并跳过引擎（绝不用近似值触发买卖）──
    _today_row = hist[hist["date"] == today_str]
    if len(_today_row) and pd.isna(_today_row.iloc[0]["pb_pct10y"]):
        logger.warning(f"今日（{today_str}）官方 PB 分位缺失，跳过引擎评估")
        if (not args.no_push and not args.report_only
                and state.get("last_pct_missing_alert") != today_str):
            notifier.send_error(
                f"⚠️ 今日（{today_str}）理杏仁官方 PB 分位缺失\n"
                f"为避免用近似值误触发，引擎已跳过本日买卖评估。\n"
                f"请手动核对（中证指数官网/东方财富），或确认理杏仁数据是否已补全。"
            )
            state["last_pct_missing_alert"] = today_str
            save_state(state)
        _update_dashboard(hist, state, ledger, config, docs_dir)
        return

    total_fen = config.get("total_fen", 150)
    cyc_start = state.get("cycle_start_date") or config.get("start_date", "2023-01-01")
    state["cycle_start_date"] = cyc_start

    # ── Step 2.3：迁移——若无策略理论账本，从历史确定性重建（一次性，不依赖 ledger）──
    if "cycle_buys" not in state:
        bs = bootstrap_cycle_from_history(hist, config, cyc_start)
        state["cycle_buys"]     = bs["cycle_buys"]
        state["cycle_sold_fen"] = bs["cycle_sold_fen"]
        state.setdefault("exited", bs.get("exited", False))
        state.setdefault("last_weekly_week", bs.get("last_weekly_week"))
        logger.info(f"迁移：重建策略理论账本 {len(state['cycle_buys'])} 笔买入")
        save_state(state)

    # ── Step 2.4：轮次重置（引擎自身已全清仓 → 自动开启新一轮，不看用户 ledger）──
    _pos = theoretical_position(state, total_fen)
    if state.get("exited") and _pos["current_fen"] == 0 and _pos["total_bought"] > 0:
        old_cycle  = state.get("cycle_id", 1)
        last_trade = state.get("exit_date") or today_str
        new_start  = (datetime.strptime(last_trade, "%Y-%m-%d")
                      + timedelta(days=1)).strftime("%Y-%m-%d")
        state.setdefault("completed_cycles", []).append(
            {"cycle_id": old_cycle, "start": cyc_start, "end": last_trade,
             "avg": round(_pos["weighted_avg"], 2) if _pos["weighted_avg"] else None})
        state.update({
            "cycle_id": old_cycle + 1,
            "cycle_start_date": new_start,
            "t1_fired": False, "t2_fired": False, "t3_fired": False,
            "rightside_used": False, "armed": False, "reduced": False,
            "exited": False, "observation_entered": False, "exit_streak": 0,
            "cycle_buys": [], "cycle_sold_fen": 0,
            "last_weekly_week": None, "phase": "waiting",
        })
        logger.info(f"上一轮(第{old_cycle}轮)已清仓，开启第{state['cycle_id']}轮，"
                    f"cycle_start_date={new_start}")
        if not args.no_push:
            notifier.send_info(
                f"🔄 第{old_cycle}轮已全部止盈清仓",
                f"系统已重置，开启第{state['cycle_id']}轮建仓监测。")
        save_state(state)

    # ── Step 2.5：对账——按(日期,动作,份数)精确匹配，逐行消费，避免一笔回填误清同日多信号 ──
    if state.get("signals_pending") and len(ledger) > 0:
        action_map = {
            'T1':'buy','T2':'buy','T3':'buy',
            'weekly_3':'buy','weekly_6':'buy','rightside':'buy',
            'reduce':'reduce','exit':'exit',
        }
        used_rows = set()
        cleaned   = []
        for sig in state["signals_pending"]:
            sig_date   = sig.get("date", "")
            sig_action = action_map.get(sig.get("type", ""), "buy")
            sig_fen    = sig.get("fen", 0)
            cand = ledger[(ledger["date"] == sig_date)
                          & (ledger["action"] == sig_action)
                          & (ledger["fen"] == sig_fen)]
            matched = next((i for i in cand.index if i not in used_rows), None)
            if matched is None:
                cleaned.append(sig)        # 未回填 → 保留提醒（不再 7 天过期）
            else:
                used_rows.add(matched)     # 此 ledger 行已被认领，避免重复匹配
        if len(cleaned) != len(state["signals_pending"]):
            logger.info(f"已核对 {len(state['signals_pending'])-len(cleaned)} 条已回填信号")
        state["signals_pending"] = cleaned

    # 叠加过滤：已确认/跳过的移除、买入类超期移除、卖出类永不过期（跨设备生效，无需 PAT）
    if state.get("signals_pending"):
        ack_keys = load_acknowledged_keys(ROOT)
        before = len(state["signals_pending"])
        state["signals_pending"] = filter_pending_signals(
            state["signals_pending"], hist["date"].tolist(), hist["date"].max(),
            ack_keys, int(config.get("pending_expire_trading_days", 7)))
        if len(state["signals_pending"]) < before:
            logger.info(f"已移除 {before - len(state['signals_pending'])} 条(已确认/买入超期)信号")

    # ── Step 3-8：引擎 + 推送 + 日报（统一异常兜底，失败即 Bark 告警）──
    try:
        cyc_start = state.get("cycle_start_date")

        # Step 3：运行策略引擎
        engine  = Engine(config, state, ledger, hist)
        signals = engine.run_daily(today_str)
        state   = engine.state
        warnings = calc_warnings(hist, today_str, state, ledger, config)

        # Step 4：推送信号（"已买X/150"用策略理论进度，与信号口径一致）
        if not args.no_push:
            ls = theoretical_position(state, total_fen)
            for sig in signals:
                notifier.send_signal(sig, today_str,
                                      ls.get("total_bought", 0), total_fen)
            if warnings:
                notifier.send_warnings(warnings, today_str)

        # Step 5：记录 pending signals + 永久触发日志（同日同类型去重）
        state.setdefault("trigger_log", [])
        for sig in signals:
            if sig["type"] in ("enter_observation",):   # 这类无需用户操作
                continue
            price_val = sig.get("price")
            entry = {
                "date": today_str,
                "type": sig["type"],
                "fen": sig.get("fen", 0),
                "price": round(float(price_val), 2) if price_val is not None else None,
                "reason": sig.get("reason", ""),
            }
            def _exists(lst):
                return any(e.get("date") == entry["date"]
                           and e.get("type") == entry["type"] for e in lst)
            if not _exists(state.get("signals_pending", [])):
                state.setdefault("signals_pending", []).append(entry.copy())
            if not _exists(state["trigger_log"]):
                state["trigger_log"].append(entry)      # 永久保留

        # Step 6：更新仪表盘数据
        _update_dashboard(hist, state, ledger, config, docs_dir, warnings=warnings)

        # Step 7：发日报（持仓/浮盈用策略理论账本，与信号口径一致）
        if not args.no_push:
            ls     = theoretical_position(state, total_fen)
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

        # Step 8：保存状态
        state["last_successful_run"] = today_str
        save_state(state)
        logger.info(f"state.json 已更新，signals={[s['type'] for s in signals]}")

    except Exception as e:
        logger.error(f"引擎/日报运行失败: {e}", exc_info=True)
        notifier.send_error(
            f"🔴 引擎运行异常（{today_str}）\n错误: {e}\n"
            f"策略状态未更新，请检查 Actions 日志。"
        )
        raise


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
