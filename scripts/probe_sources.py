# -*- coding: utf-8 -*-
"""
红利拥挤度模型 · 数据源可得性探针（只读，不写盘、不推送、不改任何业务逻辑）。

用途：同一份脚本分别在「本地」和「GitHub Actions」跑一次，对比哪些源在 CI 里能通。
      本地已实测：腾讯/蛋卷可达；理杏仁、东财、集思录、沪深交易所、中证官网全部超时。
      CI 环境网络不同（理杏仁在 CI 一直正常），需要实测确认。

运行：
  python scripts/probe_sources.py            # 全部探针
  python scripts/probe_sources.py --http     # 只跑 HTTP 探针（不装 akshare 时用）
  python scripts/probe_sources.py --json out.json

维度编号对应《红利热度方法论》六维度：
  ① 估值温度 ② 资金流入 ③ 相对动量 ④ 交易热度 ⑤ 持仓集中与重叠 ⑥ 基本面背离
"""
import os
import re
import sys
import json
import time
import argparse

import requests

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

TIMEOUT = 12
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"}


def _mask(s):
    """任何输出前先抹掉可能出现的 token（36位 uuid），防止泄漏到 CI 日志。"""
    return re.sub(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                  "<TOKEN>", str(s))


# ───────────────────────── HTTP 探针定义 ─────────────────────────
# 每项: (维度, 名称, 用途, method, url, headers, body, 期望关键字)
HTTP_TESTS = [
    # —— 对照组：本地已确认可达，用于区分"CI 网络不同"与"脚本坏了" ——
    # 同时它们本身就是 ③④ 的数据源、蛋卷是 ① 的备用源，故计入对应维度
    ("③④", "腾讯行情 K线", "指数/ETF 日线+成交量（对照组）", "GET",
     "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000922,day,2026-06-01,2026-07-30,20,",
     None, None, '"day"'),
    ("④", "腾讯 qt 实时", "ETF 成交额/现价", "GET",
     "https://qt.gtimg.cn/q=sh515080,sh510880,sh512890", None, None, "v_sh515080"),
    ("①", "蛋卷 index_eva", "PE/PB/ROE/股息率/分位（对照组）", "GET",
     "https://danjuanfunds.com/djapi/index_eva/detail/SH000922", None, None, '"yeild"'),
    ("①", "蛋卷 pe_history", "PE 历史序列（2016起周频）", "GET",
     "https://danjuanfunds.com/djapi/index_eva/pe_history/SH000922?day=all", None, None, "growths"),

    # —— ② 资金流入：本模型最关键、本地全断的一维 ——
    ("②", "集思录 ETF列表", "ETF 份额 + 溢价折价 + 成交额", "GET",
     "https://www.jisilu.cn/data/etf/etf_list/?___jsl=LST", None, None, "rows"),
    ("②", "东财 基金规模变动", "ETF 季度份额/规模变动", "GET",
     "https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=gmbd&mode=0&code=515080",
     {"Referer": "https://fundf10.eastmoney.com/"}, None, "gmbd"),
    ("②", "东财 ETF 实时列表", "全市场 ETF 行情（成交额/规模）", "GET",
     "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=20&po=1&fid=f3"
     "&fs=b:MK0021,b:MK0022,b:MK0023,b:MK0024&fields=f12,f14,f2,f3,f6,f20,f21",
     {"Referer": "https://quote.eastmoney.com/"}, None, '"diff"'),
    ("②", "上交所 ETF 列表", "沪市 ETF 份额", "GET",
     "http://query.sse.com.cn/commonQuery.do?sqlId=COMMON_SSE_FUND_LIST_SCSJ_ETFLB_L_NEW",
     {"Referer": "http://www.sse.com.cn/"}, None, "result"),
    ("②", "深交所 ETF 列表", "深市 ETF 份额", "GET",
     "https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON&CATALOGID=1105&TABKEY=tab1",
     {"Referer": "https://www.szse.cn/"}, None, "data"),

    # —— ⑤ 持仓集中与重叠 ——
    ("⑤", "中证指数 前十大权重", "指数成分权重", "GET",
     "https://www.csindex.com.cn/csindex-home/index/weight/top10/000922", None, None, "weight"),
    ("⑤", "中证指数 权重文件", "完整成分股权重表", "GET",
     "https://csi-web-dev.oss-cn-shanghai-finance-1-pub.aliyuncs.com/static/html/csindex/"
     "public/uploads/file/autofile/closeweight/000922closeweight.xls", None, None, None),

    # —— ⑥ 基本面背离 ——
    ("⑥", "巨潮 公告查询", "财报/分红公告检索", "POST",
     "http://www.cninfo.com.cn/new/hisAnnouncement/query", None,
     {"pageNum": 1, "pageSize": 5, "column": "szse", "stock": "", "searchkey": "分红",
      "category": "", "isHLtitle": "true"}, "announcements"),
]


def probe_http(item):
    dim, name, use, method, url, hdrs, body, kw = item
    headers = dict(UA)
    if hdrs:
        headers.update(hdrs)
    t0 = time.time()
    try:
        if method == "POST":
            r = requests.post(url, data=body, headers=headers, timeout=TIMEOUT)
        else:
            r = requests.get(url, headers=headers, timeout=TIMEOUT)
        dt = time.time() - t0
        ok = r.status_code == 200 and (kw is None or kw in r.text)
        sample = r.text[:120].replace("\n", " ").replace("\r", "")
        status = "✅通" if ok else f"⚠️HTTP{r.status_code}"
        if r.status_code == 200 and kw and kw not in r.text:
            status = "⚠️无预期字段"
        return {"dim": dim, "name": name, "use": use, "status": status,
                "seconds": round(dt, 1), "bytes": len(r.content), "sample": sample}
    except requests.exceptions.Timeout:
        return {"dim": dim, "name": name, "use": use, "status": "❌超时",
                "seconds": round(time.time() - t0, 1), "bytes": 0, "sample": "(疑似被墙)"}
    except Exception as e:
        return {"dim": dim, "name": name, "use": use, "status": "❌失败",
                "seconds": round(time.time() - t0, 1), "bytes": 0,
                "sample": f"{type(e).__name__}: {str(e)[:70]}"}


# ───────────────────────── 理杏仁探针（含 dyr 支持性测试） ─────────────────────────
def probe_lixinger():
    """CI 里带 LIXINGER_TOKEN。重点验证：股息率 dyr 指标是否可用（①维度 35% 权重靠它）。"""
    token = os.environ.get("LIXINGER_TOKEN", "").strip()
    out = []
    if not token:
        return [{"dim": "①⑥", "name": "理杏仁", "use": "指数基本面",
                 "status": "⏭️跳过", "seconds": 0, "bytes": 0,
                 "sample": "未设 LIXINGER_TOKEN"}]
    url = "https://open.lixinger.com/api/cn/index/fundamental"
    trials = [
        ("理杏仁 cp/pe/pb", ["cp", "pe_ttm.y10.mcw.cv", "pb.y10.mcw.cv"]),
        ("理杏仁 dyr(股息率)", ["dyr.y10.mcw.cv", "dyr.y10.mcw.cvpos"]),
        ("理杏仁 现金流指标", ["cfp.y10.mcw.cv"]),
    ]
    for name, metrics in trials:
        t0 = time.time()
        try:
            r = requests.post(url, json={"token": token, "stockCodes": ["000922"],
                                         "startDate": "2026-07-01", "endDate": "2026-07-30",
                                         "metricsList": metrics},
                              headers={"Content-Type": "application/json"}, timeout=TIMEOUT)
            j = r.json()
            ok = j.get("code") == 1 and j.get("data")
            sample = _mask(json.dumps(j, ensure_ascii=False)[:150])
            out.append({"dim": "①⑥", "name": name, "use": "、".join(metrics)[:40],
                        "status": "✅通" if ok else "⚠️返回异常",
                        "seconds": round(time.time() - t0, 1),
                        "bytes": len(r.content), "sample": sample})
        except Exception as e:
            out.append({"dim": "①⑥", "name": name, "use": "、".join(metrics)[:40],
                        "status": "❌失败", "seconds": round(time.time() - t0, 1),
                        "bytes": 0, "sample": f"{type(e).__name__}: {_mask(e)[:70]}"})
    return out


# ───────────────────────── akshare 探针 ─────────────────────────
AK_TESTS = [
    ("①", "bond_zh_us_rate", "10年期国债收益率（股债利差分母）", {}),
    ("②④", "fund_etf_spot_em", "全市场 ETF 实时（成交额/规模）", {}),
    ("②", "fund_etf_fund_daily_em", "ETF 基金净值日报", {}),
    ("⑤", "index_stock_cons_weight_csindex", "中证指数成分股权重", {"symbol": "000922"}),
    ("④", "stock_zh_a_spot_em", "全A实时（换手率）", {}),
]


def probe_akshare():
    out = []
    try:
        import akshare as ak
    except Exception as e:
        return [{"dim": "—", "name": "akshare", "use": "多项",
                 "status": "⏭️未安装", "seconds": 0, "bytes": 0,
                 "sample": f"{type(e).__name__}"}]
    ver = getattr(ak, "__version__", "?")
    for dim, fn, use, kwargs in AK_TESTS:
        t0 = time.time()
        if not hasattr(ak, fn):
            out.append({"dim": dim, "name": f"ak.{fn}", "use": use, "status": "⚠️无此函数",
                        "seconds": 0, "bytes": 0, "sample": f"akshare {ver}"})
            continue
        try:
            df = getattr(ak, fn)(**kwargs)
            cols = list(df.columns)[:6] if hasattr(df, "columns") else []
            out.append({"dim": dim, "name": f"ak.{fn}", "use": use, "status": "✅通",
                        "seconds": round(time.time() - t0, 1), "bytes": len(df),
                        "sample": f"{len(df)}行 | 列: {cols}"})
        except Exception as e:
            out.append({"dim": dim, "name": f"ak.{fn}", "use": use, "status": "❌失败",
                        "seconds": round(time.time() - t0, 1), "bytes": 0,
                        "sample": f"{type(e).__name__}: {str(e)[:70]}"})
    return out


# ───────────────────────── 主流程 ─────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", action="store_true", help="只跑 HTTP 探针，跳过 akshare")
    ap.add_argument("--json", default="", help="把结果写入 JSON 文件")
    args = ap.parse_args()

    print("=" * 118)
    print("红利拥挤度模型 · 数据源可得性探针")
    print(f"运行环境: {'GitHub Actions' if os.environ.get('GITHUB_ACTIONS') else '本地'}"
          f"   Python {sys.version.split()[0]}   时间 {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 118)

    rows = [probe_http(t) for t in HTTP_TESTS]
    rows += probe_lixinger()
    if not args.http:
        rows += probe_akshare()

    print(f"\n{'维度':<5}{'名称':<24}{'状态':<12}{'耗时':>6}  {'用途 / 返回样例'}")
    print("-" * 118)
    for r in rows:
        print(f"{r['dim']:<5}{r['name']:<24}{r['status']:<12}{r['seconds']:>5}s  "
              f"{r['use'][:30]}")
        print(f"{'':<47}{_mask(r['sample'])[:100]}")

    # 汇总：按维度判定是否解锁
    print("\n" + "=" * 118)
    print("汇总（✅ = 该维度至少有一个可用源）")
    print("-" * 118)
    by_dim = {}
    for r in rows:
        for d in re.findall(r"[①②③④⑤⑥]", r["dim"]):
            by_dim.setdefault(d, []).append(r["status"].startswith("✅"))
    names = {"①": "估值温度(20%)", "②": "资金流入(20%)", "③": "相对动量(15%)",
             "④": "交易热度(15%)", "⑤": "持仓集中与重叠(10%)", "⑥": "基本面背离(20%)"}
    for d in "①②③④⑤⑥":
        got = by_dim.get(d, [])
        mark = "✅ 有可用源" if any(got) else ("❌ 全部不可达" if got else "— 未探测")
        print(f"  {d} {names[d]:<22} {mark}   （探测 {len(got)} 项，通 {sum(got)} 项）")
    print("=" * 118)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        print(f"结果已写入 {args.json}")


if __name__ == "__main__":
    main()
