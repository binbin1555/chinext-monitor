# -*- coding: utf-8 -*-
"""
均值回归假设哨兵 · 决定性历史检验（只读，不写业务数据、不推送）。

背景：哨兵用两个量判断"跌下去会涨回来"这个前提还成不成立——
      隐含盈利 E = 收盘点位 ÷ PE-TTM      股利支付率 = 股息率 × PE-TTM
      2018 那轮（纯估值杀）它正确地保持了沉默。
      但真正要检验的是 2010-01 → 2014-06 那次 −38.6% 的下跌：
      如果哨兵在那段也沉默，说明它对最该报警的场景无效，应当放弃。

问题：蛋卷/本地 CSV 的估值历史都只到 2016-07，够不着 2010。
      理杏仁是唯一可能有更长历史的源，且只能在 CI 里访问。

本脚本做三件事：
  1. 边界探测——逐年试探理杏仁 000922 估值数据的最早可得日期
  2. 按年分段拉取 cp / pe_ttm / dyr，拼成完整序列
  3. 直接算出 E、股利支付率，打印年度快照 + 2010-2014 专项分析

运行：
  python scripts/sentinel_history.py                    # 需环境变量 LIXINGER_TOKEN
  python scripts/sentinel_history.py --csv out.csv      # 同时导出 CSV
  python scripts/sentinel_history.py --probe-only       # 只做边界探测，不拉全量
"""
import os
import re
import sys
import time
import json
import argparse
from datetime import date

import requests

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

URL = "https://open.lixinger.com/api/cn/index/fundamental"
CODE = "000922"
TIMEOUT = 30

# .cv（当前值）与窗口长度无关，y10 取不到时用 y5 兜底——2010 年时理杏仁自身
# 可能凑不出 10 年窗口而拒绝 y10 指标。
METRIC_SETS = [
    ["cp", "pe_ttm.y10.mcw.cv", "dyr.y10.mcw.cv"],
    ["cp", "pe_ttm.y5.mcw.cv", "dyr.y5.mcw.cv"],
    ["cp", "pe_ttm.mcw.cv", "dyr.mcw.cv"],
    ["cp", "pe_ttm.y10.mcw.cv"],
]


def mask(s):
    return re.sub(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                  r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", "<TOKEN>", str(s))


def call(token, start, end, metrics):
    """返回 (rows, err)。rows 为 list[dict]，err 为 None 或错误说明。"""
    body = {"token": token, "stockCodes": [CODE],
            "startDate": start, "endDate": end, "metricsList": metrics}
    try:
        r = requests.post(URL, json=body, timeout=TIMEOUT,
                          headers={"Content-Type": "application/json"})
        j = r.json()
    except Exception as e:
        return [], f"{type(e).__name__}: {mask(e)[:80]}"
    if j.get("code") != 1:
        return [], mask(json.dumps(j, ensure_ascii=False))[:160]
    return j.get("data") or [], None


def pick(row, prefix):
    """从返回行里取出以 prefix 开头的那个指标值（兼容 y10/y5/无窗口三种写法）。"""
    for k, v in row.items():
        if k.startswith(prefix) and v is not None:
            return float(v)
    return None


def probe_earliest(token):
    """逐年探测最早可得数据。返回 (最早年份, 可用的 metrics 组合)。"""
    print("【第一步】边界探测：理杏仁 000922 估值数据能回溯到哪一年？")
    print(f"{'年份':<8}{'指标组':<34}{'返回条数':>8}  说明")
    print("-" * 96)
    earliest, good_metrics = None, None
    for y in range(2005, 2018):
        hit = False
        for mi, metrics in enumerate(METRIC_SETS):
            rows, err = call(token, f"{y}-01-01", f"{y}-03-31", metrics)
            tag = "+".join(m.split(".")[0] for m in metrics[1:]) or "cp"
            if rows:
                # 必须真的带回 pe，只有 cp 没意义
                has_pe = any(pick(r, "pe_ttm") for r in rows)
                print(f"{y:<8}{tag:<34}{len(rows):>8}  "
                      f"{'含PE ✅' if has_pe else '仅cp，无PE'}")
                if has_pe:
                    hit = True
                    if earliest is None:
                        earliest, good_metrics = y, metrics
                    break
            elif mi == len(METRIC_SETS) - 1:
                print(f"{y:<8}{tag:<34}{0:>8}  {err[:50] if err else '无数据'}")
            time.sleep(0.15)
        if hit and earliest is not None and y >= earliest + 1:
            break
    print()
    if earliest is None:
        print("❌ 所有年份都取不到含 PE 的数据——理杏仁这条路走不通。")
    else:
        print(f"✅ 最早可得含 PE 的年份：{earliest}   使用指标组：{good_metrics}")
    return earliest, good_metrics


def fetch_all(token, start_year, metrics):
    print(f"\n【第二步】按年分段拉取 {start_year}-01-01 → 今天")
    out = {}
    for y in range(start_year, date.today().year + 1):
        s, e = f"{y}-01-01", f"{y}-12-31"
        rows, err = call(token, s, e, metrics)
        if not rows:
            print(f"  {y}  ✗ {err[:60] if err else '无数据'}")
            continue
        n = 0
        for r in rows:
            d = (r.get("date") or "")[:10]
            cp, pe, dyr = r.get("cp"), pick(r, "pe_ttm"), pick(r, "dyr")
            if d and cp and pe:
                out[d] = (float(cp), float(pe), dyr)
                n += 1
        print(f"  {y}  ✓ {n} 个交易日")
        time.sleep(0.2)
    return out


def analyze(data, csv_path=None):
    import pandas as pd
    df = pd.DataFrame([(d, v[0], v[1], v[2]) for d, v in sorted(data.items())],
                      columns=["date", "cp", "pe", "dyr"])
    df["E"] = df.cp / df.pe
    df["payout"] = df.dyr * df.pe if df.dyr.notna().any() else None
    if csv_path:
        df.to_csv(csv_path, index=False, float_format="%.4f")
        print(f"\n已导出 {csv_path}（{len(df)} 行）")

    print(f"\n【第三步】年度快照   数据范围 {df.date.iloc[0]} → {df.date.iloc[-1]}")
    print(f"{'年末':<12}{'点位':>8}{'PE':>7}{'股息率':>8}{'隐含盈利E':>10}"
          f"{'E同比':>9}{'股利支付率':>10}")
    print("-" * 70)
    prev_e = {}
    for y in range(int(df.date.iloc[0][:4]), int(df.date.iloc[-1][:4]) + 1):
        s = df[df.date.str[:4] == str(y)]
        if len(s) == 0:
            continue
        r = s.iloc[-1]
        pe_yoy = ""
        if (y - 1) in prev_e and prev_e[y - 1]:
            pe_yoy = f"{(r.E / prev_e[y-1] - 1) * 100:+.1f}%"
        prev_e[y] = r.E
        dy = f"{r.dyr*100:6.2f}%" if r.dyr == r.dyr else "     —"
        po = f"{r.payout*100:8.1f}%" if r.payout == r.payout else "       —"
        print(f"{r.date:<12}{r.cp:8.0f}{r.pe:7.2f}{dy:>8}{r.E:10.1f}"
              f"{pe_yoy:>9}{po:>10}")

    # —— 专项：2010-01 → 2014-06 那次 −38.6% ——
    print("\n【第四步】专项：2010-01 → 2014-06（历史上唯一已知的『红利深套』）")
    seg = df[(df.date >= "2010-01-01") & (df.date <= "2014-06-30")]
    if len(seg) < 100:
        print("  ⚠️ 该区间数据不足，无法检验——哨兵对这个场景仍是未经验证的。")
        return
    a, b = seg.iloc[0], seg.iloc[-1]
    print(f"  区间: {a.date} → {b.date}   点位 {a.cp:.0f} → {b.cp:.0f} "
          f"（{(b.cp/a.cp-1)*100:+.1f}%）")
    print(f"  PE:   {a.pe:.2f} → {b.pe:.2f}（{(b.pe/a.pe-1)*100:+.1f}%）  ← 估值杀了多少")
    print(f"  E:    {a.E:.1f} → {b.E:.1f}（{(b.E/a.E-1)*100:+.1f}%）  ← 盈利塌了没有")
    if seg.payout.notna().any():
        print(f"  股利支付率: {a.payout*100:.1f}% → {b.payout*100:.1f}%")
    print()
    print("  逐半年 E 走势（判断是否出现『连续负增长』这种哨兵会报警的形态）：")
    marks = [f"{y}-{m}" for y in range(2010, 2015) for m in ("06-30", "12-31")]
    prev = None
    for mk in marks:
        s = df[df.date <= mk]
        if len(s) == 0 or mk > df.date.iloc[-1]:
            continue
        r = s.iloc[-1]
        ch = f"{(r.E/prev - 1)*100:+6.1f}%" if prev else "     —"
        prev = r.E
        print(f"    {r.date}  点位 {r.cp:7.0f}   PE {r.pe:5.2f}   E {r.E:8.1f}   半年变化 {ch}")
    print("\n  判读要点：若 E 在整段下跌中保持上升或持平 → 那是估值杀，哨兵会沉默，")
    print("            说明它对『红利深套』这一最关键场景无效，应当放弃或改测分红绝对额。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="", help="导出 CSV 路径")
    ap.add_argument("--probe-only", action="store_true", help="只做边界探测")
    args = ap.parse_args()

    token = os.environ.get("LIXINGER_TOKEN", "").strip()
    if not token:
        print("❌ 未设置 LIXINGER_TOKEN，无法运行（本地被墙，请在 CI 里跑）")
        sys.exit(1)

    print("=" * 96)
    print("均值回归假设哨兵 · 决定性历史检验")
    print(f"环境: {'GitHub Actions' if os.environ.get('GITHUB_ACTIONS') else '本地'}"
          f"   时间 {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 96)

    earliest, metrics = probe_earliest(token)
    if earliest is None:
        sys.exit(0)
    if earliest > 2010:
        print(f"\n⚠️ 最早只到 {earliest} 年，够不着 2010-2014——"
              f"决定性检验做不了，哨兵将保持『未经验证』状态。")
    if args.probe_only:
        return

    data = fetch_all(token, min(earliest, 2009), metrics)
    if not data:
        print("未取到任何数据")
        sys.exit(0)
    analyze(data, args.csv or None)


if __name__ == "__main__":
    main()
