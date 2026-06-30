"""
一次性初始化脚本：从本地已有 CSV 文件生成 data/history.csv 和 docs/data.json。
无需 API 密钥，用于首次部署时立刻让仪表盘显示历史数据。

运行：python src/init_from_csv.py
"""
import sys
import json
import logging
import numpy as np
import pandas as pd
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import yaml
from src.fetch    import load_seed_from_local, compute_percentiles, compute_mas, HISTORY_COLS
from src.engine   import recalc_from_ledger
from src.metrics  import build_nav_series, calc_performance, calc_gaps
from src.dashboard import generate_data_json

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main():
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    with open(ROOT / "state.json", encoding="utf-8") as f:
        state = json.load(f)

    data_dir = ROOT / "data"
    docs_dir = ROOT / "docs"

    # ── 1. 从本地 CSV 加载 ──
    logger.info("从本地 CSV 文件加载历史数据…")
    hist = load_seed_from_local(data_dir)
    if hist is None or len(hist) == 0:
        logger.error("未找到本地 CSV 文件，请确认在同一目录下有理杏仁导出的 PB/PE/MA 文件")
        sys.exit(1)
    logger.info(f"加载到 {len(hist)} 行原始数据，日期范围 {hist['date'].min()} ~ {hist['date'].max()}")

    # ── 2. 计算均线；分位优先用理杏仁官方"分位点"列，仅缺失才本地兜底 ──
    logger.info("计算移动均线…")
    hist = compute_mas(hist)
    if hist["pb_pct10y"].isna().any() or hist["pe_pct10y"].isna().any():
        logger.info("部分分位缺失，本地兜底计算…")
        hist = compute_percentiles(hist)
    else:
        logger.info("已采用理杏仁官方近10年分位，无需本地计算")

    # ── 3. 保存 history.csv ──
    data_dir.mkdir(parents=True, exist_ok=True)
    csv_path = data_dir / "history.csv"
    hist.to_csv(csv_path, index=False, float_format="%.4f")
    logger.info(f"history.csv 已保存 → {csv_path}")

    # ── 4. 加载流水 ──
    ledger_path = ROOT / "ledger.csv"
    if ledger_path.exists() and ledger_path.stat().st_size > 50:
        ledger = pd.read_csv(ledger_path, dtype={"date": str, "action": str,
                                                   "fen": int, "price": float, "note": str})
        logger.info(f"ledger.csv 已加载，{len(ledger)} 条流水")
    else:
        ledger = pd.DataFrame(columns=["date", "action", "fen", "price", "note"])

    # ── 5. 生成仪表盘数据 ──
    logger.info("计算净值序列…")
    nav_df = build_nav_series(hist, ledger, config, state)
    nav_arr = nav_df["nav_portfolio"].tolist() if len(nav_df) > 0 else [1.0]
    perf   = calc_performance(nav_arr, config.get("start_date", "2023-04-24"),
                               config.get("cash_rate_annual", 0.02))
    today  = hist["date"].max()
    gaps   = calc_gaps(hist, today, state, ledger, config)

    logger.info("生成 docs/data.json…")
    generate_data_json(
        hist=hist, state=state, ledger=ledger, config=config,
        metrics_perf=perf, nav_df=nav_df,
        gaps=gaps, warnings=[],
        docs_dir=docs_dir,
    )

    # ── 6. 汇报 ──
    ls = recalc_from_ledger(ledger, config.get("total_fen", 150))
    latest = hist[hist["date"] == today].iloc[0] if today in hist["date"].values else None
    sep = "=" * 55
    print(f"\n{sep}")
    print("  初始化完成！仪表盘数据已就绪。")
    print(sep)
    if latest is not None:
        pb_pct = latest["pb_pct10y"]
        print(f"  最新日期  : {today}")
        try:
            print(f"  创业板收盘: {latest['close']:.0f}")
            if not np.isnan(pb_pct):
                print(f"  PB近10年分位: {pb_pct*100:.1f}%")
            if not np.isnan(latest['ma120']):
                print(f"  MA120     : {latest['ma120']:.0f}")
        except Exception:
            pass
    if ls.get("weighted_avg") and latest is not None:
        try:
            fp = float(latest["close"]) / ls["weighted_avg"] - 1
            print(f"  均价      : {ls['weighted_avg']:.0f}   浮盈: {fp*100:.1f}%")
        except Exception:
            pass
    if perf.get("cagr") is not None:
        print(f"\n  CAGR: {perf['cagr']*100:.1f}%  回撤: {perf['max_drawdown']*100:.1f}%")
    print("\n  本地预览命令：")
    print("    python3 -m http.server 7700 --directory docs")
    print("  然后访问 http://localhost:7700")


if __name__ == "__main__":
    main()
