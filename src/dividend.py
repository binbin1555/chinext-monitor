"""
中证红利 分批建仓监测（独立模块，不触碰创业板逻辑）。

策略（用户 2026-07 定稿）:
  买入: 价格指数(000922)跌破 价格MA250 的 -4%/-7%/-10%, 各买 1/3 (等权分批)
  卖出: 价格涨回 MA250, 全部卖出, 清零重来
  记账口径: 价格指数(信号与展示均用价格; 全收益仅另计分红, 不在信号链路)

数据: 价格指数 000922 收盘。理杏仁 cp 为主, 腾讯为备援并交叉校验（已验证与理杏仁CSV逐日一致）。

运行:
  python src/dividend.py                 # 抓取+判断+推送（Bark）
  python src/dividend.py --no-push       # 不推送
  python src/dividend.py --dry-run       # 不写盘不推送, 仅打印
  python src/dividend.py --source tencent # 强制用腾讯源（本地无理杏仁网络时）
"""
import os
import re
import sys
import json
import glob
import logging
import argparse
from pathlib import Path
from datetime import date, datetime, timedelta

import pandas as pd
import requests

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.fetch import fetch_lixinger_close
from src.notify import BarkNotifier

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("dividend")

# ─────────── 策略参数 ───────────
CODE       = "000922"          # 中证红利 价格指数
MA_WINDOW  = 250
TRANCHES   = [0.04, 0.07, 0.10]  # 三档折价 -4/-7/-10, 各 1/3
WEIGHT     = 1.0 / 3
TRANCHE_LABEL = {0: "-4%", 1: "-7%", 2: "-10%"}
NEAR_PCT   = 0.015               # 距触发 ≤1.5% 视为"临近"（此期间每天推一次）
WEEKLY_WEEKDAY = 4               # 每周五(周一=0)收盘后发一条周报

HIST_PATH  = ROOT / "data" / "dividend_history.csv"
STATE_PATH = ROOT / "docs" / "dividend.json"   # 兼作变化检测状态 + 独立红利页面数据源
SEED_GLOB  = str(ROOT / "中证红利_股息率_市值加权_*.csv")
BARK_GROUP = "中证红利择时"                # Bark 分组：与创业板("创业板择时")分开，手机上分门别类
_DASH = os.environ.get("DASHBOARD_URL", "").rstrip("/")
DIVIDEND_URL = (_DASH + "/dividend.html") if _DASH else None   # 点开推送直达红利页面


def _load_token():
    """理杏仁 token：优先环境变量（CI 用）；本地无 env 时从 gitignore 的指南文件自动读取。"""
    tok = os.environ.get("LIXINGER_TOKEN", "").strip()
    if tok:
        return tok
    for f in glob.glob(str(ROOT / "理杏仁API*.txt")):
        try:
            m = re.search(r"Token:\s*([0-9a-fA-F-]{36})", open(f, encoding="utf-8").read())
            if m:
                logger.info("理杏仁 token 从本地指南文件载入（未设环境变量）")
                return m.group(1)
        except Exception:
            pass
    return ""


# ─────────── 数据抓取 ───────────
def fetch_tencent(days=90):
    """腾讯日线备援, 返回 {iso_date: close}。已验证与理杏仁一致。"""
    end = date.today().strftime("%Y-%m-%d")
    start = (date.today() - timedelta(days=days * 2)).strftime("%Y-%m-%d")
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param=sh{CODE},day,{start},{end},{days},")
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    node = r.json()["data"][f"sh{CODE}"]
    kl = node.get("day") or node.get("qfqday") or []
    return {row[0][:10]: float(row[2]) for row in kl if row and row[2] not in (None, "")}


def drop_unsettled(prices):
    """盘中运行时丢弃"当日未收盘"的价格，避免用盘中实时价触发错误信号。
    生产环境收盘后(北京21:15)运行不受影响；主要保护盘中手动/本地运行。"""
    now_bj = datetime.utcnow() + timedelta(hours=8)          # 北京时间
    today_bj = now_bj.strftime("%Y-%m-%d")
    settled = (now_bj.hour > 15) or (now_bj.hour == 15 and now_bj.minute >= 30)
    if (today_bj in prices) and not settled:
        prices = {d: c for d, c in prices.items() if d != today_bj}
        logger.info(f"盘中运行，已丢弃当日未收盘数据 {today_bj}（收盘后自动计入）")
    return prices


def fetch_prices(source="auto", token=None, days=90):
    """
    返回 (prices{iso:close}, source_used, crosscheck_note)。
    理杏仁 cp 为主, 腾讯备援; 两者都有时交叉校验（分歧>1点告警）。
    """
    end = date.today().strftime("%Y-%m-%d")
    start = (date.today() - timedelta(days=days * 2)).strftime("%Y-%m-%d")
    lx, tx = {}, {}

    if source in ("auto", "lixinger") and token:
        try:
            lx = fetch_lixinger_close(token, CODE, start, end)
            lx = {d: c for d, c in lx.items() if c is not None}
        except Exception as e:
            logger.warning(f"理杏仁抓取失败, 将用腾讯备援: {e}")

    if source in ("auto", "tencent") or not lx:
        try:
            tx = fetch_tencent(days)
        except Exception as e:
            logger.warning(f"腾讯抓取失败: {e}")

    note = ""
    if lx and tx:
        common = [d for d in lx if d in tx and lx[d] and tx[d]]
        maxdiff = max((abs(lx[d] - tx[d]) for d in common), default=0.0)
        note = f"双源交叉校验 {len(common)} 天, 最大差 {maxdiff:.2f} 点"
        if maxdiff > 1.0:
            logger.warning("⚠️ 理杏仁与腾讯分歧较大: " + note)
        else:
            logger.info("✓ " + note)

    if lx:
        return lx, "lixinger", note
    if tx:
        return tx, "tencent", note
    return {}, "none", "无可用数据源"


# ─────────── 历史缓存 ───────────
def seed_from_csv():
    """首次运行: 用理杏仁导出的 CSV 播种 10 年价格历史。"""
    files = sorted(glob.glob(SEED_GLOB))
    if not files:
        return pd.DataFrame(columns=["date", "close"])
    import csv as csvmod
    rows = []
    with open(files[-1], encoding="utf-8-sig") as f:
        rd = csvmod.reader(f)
        next(rd, None)
        for row in rd:
            if row and re.match(r"^\d{4}/\d{1,2}/\d{1,2}$", (row[0] or "").strip()):
                y, m, d = row[0].strip().split("/")
                iso = f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
                try:
                    rows.append((iso, float(row[1])))
                except (ValueError, IndexError):
                    pass
    df = pd.DataFrame(rows, columns=["date", "close"]).sort_values("date")
    logger.info(f"从 CSV 播种 {len(df)} 行价格历史")
    return df.reset_index(drop=True)


def load_history():
    if HIST_PATH.exists():
        return pd.read_csv(HIST_PATH, dtype={"date": str})
    logger.info("dividend_history.csv 不存在, 从 CSV 播种…")
    return seed_from_csv()


def merge_history(df, new_prices):
    m = {r.date: float(r.close) for r in df.itertuples()}
    added = 0
    for d, c in new_prices.items():
        if c is None:
            continue
        if d not in m:
            added += 1
        m[d] = float(c)
    out = pd.DataFrame(sorted(m.items()), columns=["date", "close"])
    return out, added


# ─────────── 策略引擎 ───────────
def run_strategy(df):
    """
    在完整价格历史上跑机械策略, 返回 (df_with_ma, actions, position)。
    actions: [{date, kind:'buy'/'sell', tranche?, close, ma, entries?}]
    position: {filled:[bool*3], entries:[(date,tranche,close)]}
    """
    df = df.copy()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    df["ma"] = df["close"].rolling(MA_WINDOW).mean()

    filled = [False, False, False]
    entries = []
    actions = []
    for r in df.dropna(subset=["ma"]).itertuples():
        if any(filled) and r.close >= r.ma:          # 涨回 MA250 → 全部卖出
            actions.append({"date": r.date, "kind": "sell", "close": r.close,
                            "ma": r.ma, "entries": list(entries)})
            filled = [False, False, False]
            entries = []
            continue
        disc = r.close / r.ma - 1
        for i, d in enumerate(TRANCHES):
            if not filled[i] and disc <= -d:         # 跌破第 i 档 → 买 1/3
                filled[i] = True
                entries.append((r.date, i, r.close))
                actions.append({"date": r.date, "kind": "buy", "tranche": i,
                                "close": r.close, "ma": r.ma})
    return df, actions, {"filled": filled, "entries": entries}


def compute_status(df, pos):
    """当前状态 + 下一步触发价位（供推送 + 页面渲染）。"""
    last = df.dropna(subset=["ma"]).iloc[-1]
    close, ma = float(last.close), float(last.ma)
    disc = close / ma - 1
    filled, entries = pos["filled"], pos["entries"]
    st = {"date": last.date, "close": close, "ma": ma, "disc": disc,
          "filled": sum(filled), "filled_list": list(filled),
          "entries": [{"date": e[0], "tranche": e[1], "price": round(e[2], 1)}
                      for e in entries]}
    # 三档买入线（当前价位）+ 各档是否已买
    st["buy_lines"] = [{"tranche": i + 1, "pct": int(TRANCHES[i] * 100),
                        "level": ma * (1 - TRANCHES[i]), "filled": bool(filled[i])}
                       for i in range(3)]
    if any(filled):
        shares = sum(WEIGHT / e[2] for e in entries)
        avg = (len(entries) * WEIGHT) / shares
        st["avg_cost"] = avg
        st["ret"] = close / avg - 1
        st["sell_line"] = ma
        st["to_sell"] = ma / close - 1        # 还需涨多少到卖点
        nxt = [i for i in range(3) if not filled[i]]
        if nxt:
            st["next_buy_tranche"] = nxt[0]
            st["next_buy_line"] = ma * (1 - TRANCHES[nxt[0]])
            st["to_next_buy"] = st["next_buy_line"] / close - 1
    else:
        st["next_buy_tranche"] = 0
        st["next_buy_line"] = ma * (1 - TRANCHES[0])
        st["to_next_buy"] = st["next_buy_line"] / close - 1
    return st


# ─────────── 状态持久化 & 变化检测 ───────────
def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def build_page_data(df, status, actions, source, note):
    """写给 docs/dividend.json：既是变化检测状态，也是独立红利页面的数据源。"""
    last_action = actions[-1] if actions else None
    sub = df.dropna(subset=["ma"]).tail(260)           # 近一年价格/MA250 供画图
    series = [{"d": r.date, "c": round(float(r.close), 1), "m": round(float(r.ma), 1)}
              for r in sub.itertuples()]

    def _aid(a):   # 每个操作的稳定ID（供页面 localStorage 记住"已确认"）
        return a["date"] + "-" + a["kind"] + ("-t" + str(a["tranche"]) if a["kind"] == "buy" else "")

    recent = []
    for a in actions[-10:]:
        it = {"id": _aid(a), "date": a["date"], "kind": a["kind"], "close": round(a["close"], 1)}
        if a["kind"] == "buy":
            it["tranche"] = a["tranche"]
        else:                                          # sell：附带本轮实现收益与档数
            ents = a.get("entries", [])
            sh = sum(WEIGHT / e[2] for e in ents)
            avg = (len(ents) * WEIGHT) / sh if sh else a["close"]
            it["ret_pct"] = round((a["close"] / avg - 1) * 100, 2)
            it["n"] = len(ents)
        recent.append(it)

    # 本轮ID = 当前持仓的首笔建仓日（空仓则为 None，表示等待新一轮）
    round_id = status["entries"][0]["date"] if status.get("entries") else None
    return {
        "strategy": "买 跌破MA250 −4%/−7%/−10% 各1/3 · 卖 涨回MA250清仓（全价格口径）",
        "updated": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "data_date": status["date"],
        "source": source,
        "crosscheck": note,
        "close": round(status["close"], 1),
        "ma250": round(status["ma"], 1),
        "disc_pct": round(status["disc"] * 100, 2),
        "held": status["filled"],
        "filled_list": status["filled_list"],
        "avg_cost": round(status["avg_cost"], 1) if "avg_cost" in status else None,
        "unreal_ret_pct": round(status["ret"] * 100, 2) if "ret" in status else None,
        "sell_line": round(status["sell_line"], 1) if "sell_line" in status else None,
        "to_sell_pct": round(status["to_sell"] * 100, 2) if "to_sell" in status else None,
        "next_buy_tranche": status.get("next_buy_tranche"),
        "next_buy_line": round(status["next_buy_line"], 1) if "next_buy_line" in status else None,
        "to_next_buy_pct": round(status["to_next_buy"] * 100, 2) if "to_next_buy" in status else None,
        "buy_lines": [{"tranche": b["tranche"], "pct": b["pct"],
                       "level": round(b["level"], 1), "filled": b["filled"]}
                      for b in status["buy_lines"]],
        "entries": status["entries"],
        "recent_actions": recent,
        "last_action": recent[-1] if recent else None,   # 供页面强提醒 + 确认
        "round_id": round_id,
        "series": series,
        "last_action_date": last_action["date"] if last_action else None,
        "last_action_kind": last_action["kind"] if last_action else None,
    }


# ─────────── Bark 消息 ───────────
def push_buy(notifier, action, status):
    t = action["tranche"]
    held = status["filled"]
    lines = [
        f"跌破 MA250 的 {TRANCHE_LABEL[t]}，触发第{t+1}档",
        f"建议买入 1/3（当前已买 {held}/3）",
        f"点位 {action['close']:.1f}  MA250 {action['ma']:.1f}",
    ]
    if "next_buy_line" in status and status["filled"] < 3:
        lines.append(f"下一档 -{int(TRANCHES[status['next_buy_tranche']]*100)}% 在 {status['next_buy_line']:.1f}")
    lines.append(f"日期 {action['date']}")
    notifier._send(title=f"🟢 中证红利·买入第{t+1}档（{action['date']}）",
                   body="\n".join(lines), level="timeSensitive", group=BARK_GROUP, url=DIVIDEND_URL)


def push_sell(notifier, action):
    entries = action.get("entries", [])
    shares = sum(WEIGHT / e[2] for e in entries) if entries else 0
    cost = len(entries) * WEIGHT
    avg = cost / shares if shares else action["close"]
    ret = action["close"] / avg - 1 if avg else 0
    lines = [
        "价格涨回 MA250，触发全部止盈",
        f"卖出点位 {action['close']:.1f}（MA250 {action['ma']:.1f}）",
        f"本轮买入 {len(entries)}/3 档，价格口径收益 {ret*100:+.1f}%（不含分红，实际略高）",
        f"日期 {action['date']}",
    ]
    notifier._send(title=f"🔴 中证红利·止盈清仓（{action['date']}）",
                   body="\n".join(lines), level="timeSensitive", group=BARK_GROUP, url=DIVIDEND_URL)


def is_near(st):
    """距任一触发点 ≤ NEAR_PCT 视为临近。"""
    ns = ("to_sell" in st) and (0 < st["to_sell"] <= NEAR_PCT)
    nb = ("to_next_buy" in st) and (abs(st["to_next_buy"]) <= NEAR_PCT)
    return ns or nb


def push_near(notifier, st):
    """临近提醒：距触发 ≤1.5% 时每天推一次，便于盯盘不错过。"""
    lines = []
    if ("to_sell" in st) and (0 < st["to_sell"] <= NEAR_PCT):
        lines.append(f"再涨 {st['to_sell']*100:.1f}% 到 {st['sell_line']:.0f} → 止盈清仓（全卖）")
    if ("to_next_buy" in st) and (abs(st["to_next_buy"]) <= NEAR_PCT):
        t = st.get("next_buy_tranche", 0)
        lines.append(f"再跌 {abs(st['to_next_buy'])*100:.1f}% 到 {st['next_buy_line']:.0f} → 买入第{t+1}档（+1/3）")
    if not lines:
        return
    body = "临近触发，请留意盘中：\n" + "\n".join(lines) + \
           f"\n现价 {st['close']:.0f} · 年线 {st['ma']:.0f} · {st['date']}"
    notifier._send(title=f"🟡 中证红利·临近提醒（{st['date']}）",
                   body=body, level="active", group=BARK_GROUP, url=DIVIDEND_URL)


def push_weekly(notifier, st):
    """每周五周报：当前状态 + 距两个触发点多远（兼作系统心跳）。"""
    head = f"持仓 {st['filled']}/3"
    if "ret" in st:
        head += f" · 浮盈 {st['ret']*100:+.1f}%"
    lines = [head]
    if "to_sell" in st:
        lines.append(f"距止盈：再涨 {st['to_sell']*100:.1f}%（涨回年线 {st['sell_line']:.0f}）")
    if "to_next_buy" in st:
        t = st.get("next_buy_tranche", 0)
        lines.append(f"距加仓：再跌 {abs(st['to_next_buy'])*100:.1f}%（到 {st['next_buy_line']:.0f} 买第{t+1}档）")
    if st["filled"] == 0:
        lines.append(f"空仓等待，首档买入线 {st['next_buy_line']:.0f}")
    lines.append(f"现价 {st['close']:.0f} · 年线 {st['ma']:.0f} · {st['date']}")
    notifier._send(title=f"📄 中证红利·周报（{st['date']}）",
                   body="\n".join(lines), level="active", group=BARK_GROUP, url=DIVIDEND_URL)


def print_status(status, source, note):
    print("=" * 60)
    print(f"中证红利 · 分批 -4/-7/-10 · 涨回MA250止盈   [{source}]")
    print(f"数据日 {status['date']}   {note}")
    print("-" * 60)
    print(f"价格 {status['close']:.1f}   MA250 {status['ma']:.1f}   折价 {status['disc']*100:+.2f}%")
    if status["filled"] > 0:
        print(f"持仓 {status['filled']}/3   加权成本(价格) {status['avg_cost']:.1f}   "
              f"浮盈 {status['ret']*100:+.2f}%")
        print(f"卖出线(涨回MA250) {status['sell_line']:.1f}   还需涨 {status['to_sell']*100:+.2f}%")
        if "next_buy_line" in status:
            print(f"下一档 -{int(TRANCHES[status['next_buy_tranche']]*100)}% 在 "
                  f"{status['next_buy_line']:.1f}（还需跌 {status['to_next_buy']*100:+.2f}%）")
    else:
        print(f"空仓等待   首档 -4% 触发价 {status['next_buy_line']:.1f}"
              f"（还需跌 {status['to_next_buy']*100:+.2f}%）")
    print("=" * 60)


# ─────────── 主流程 ───────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-push", action="store_true", help="不发 Bark")
    ap.add_argument("--dry-run", action="store_true", help="不写盘不推送, 仅打印")
    ap.add_argument("--source", default="auto", choices=["auto", "lixinger", "tencent"])
    args = ap.parse_args()

    token = _load_token()
    bark_key = os.environ.get("BARK_KEY", "").strip()
    notifier = BarkNotifier(bark_key)

    try:
        hist = load_history()
        prices, source, note = fetch_prices(args.source, token)
        if not prices:
            logger.error("无数据, 退出")
            if not args.no_push and not args.dry_run:
                notifier._send("🔴 中证红利监测异常", "理杏仁与腾讯均抓取失败",
                               level="timeSensitive", group=BARK_GROUP, url=DIVIDEND_URL)
            sys.exit(1)
        prices = drop_unsettled(prices)                # 盘中丢弃未收盘当日, 防错误信号
        hist, added = merge_history(hist, prices)
        logger.info(f"数据源 {source}; 新增 {added} 个交易日; 历史共 {len(hist)} 行")

        df, actions, pos = run_strategy(hist)
        status = compute_status(df, pos)
        print_status(status, source, note)

        prev = load_state()
        prev_last = prev.get("last_action_date")
        prev_hb = prev.get("last_heartbeat_date")
        new_actions = [a for a in actions if (prev_last is None) or (a["date"] > prev_last)]
        first_run = not STATE_PATH.exists()
        data_date = status["date"]
        can_push = (not args.no_push) and (not args.dry_run)
        weekday = datetime.strptime(data_date, "%Y-%m-%d").weekday()

        pushed = False
        if first_run:
            logger.info(f"首次运行, 建立基线（不推送, 不补发历史 {len(actions)} 个动作）")
        elif new_actions:                                  # ① 触发：强提醒（必推）
            logger.info(f"检测到 {len(new_actions)} 个新动作, 推送(强提醒)")
            for a in new_actions:
                if can_push:
                    push_buy(notifier, a, status) if a["kind"] == "buy" else push_sell(notifier, a)
                else:
                    logger.info(f"[未推送] {a['date']} {a['kind']} t={a.get('tranche')} {a['close']:.1f}")
            pushed = True
        elif prev_hb == data_date:                         # 同一交易日已推过临近/周报 → 防重复
            logger.info("今日已推过临近/周报, 静默")
        elif is_near(status):                              # ② 临近：≤1.5% 每天推一次
            logger.info("临近触发(≤1.5%), 推送临近提醒")
            if can_push:
                push_near(notifier, status)
            else:
                logger.info("[未推送] 临近提醒")
            pushed = True
        elif weekday == WEEKLY_WEEKDAY:                    # ③ 周五周报（兼心跳）
            logger.info("周五, 推送周报")
            if can_push:
                push_weekly(notifier, status)
            else:
                logger.info("[未推送] 周报")
            pushed = True
        else:
            logger.info("无触发/临近/周报, 静默")

        if not args.dry_run:
            HIST_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            hist.to_csv(HIST_PATH, index=False, float_format="%.4f")
            page = build_page_data(df, status, actions, source, note)
            # 记录本交易日已推送（含触发/临近/周报），供同日多次运行防重复
            page["last_heartbeat_date"] = data_date if (pushed and can_push) else prev_hb
            save_state(page)
            logger.info("已写盘 data/dividend_history.csv / docs/dividend.json")

    except Exception as e:
        logger.exception("中证红利监测异常")
        if not args.no_push and not args.dry_run:
            notifier._send("🔴 中证红利监测异常", str(e)[:300],
                           level="timeSensitive", group=BARK_GROUP, url=DIVIDEND_URL)
        sys.exit(1)


if __name__ == "__main__":
    main()
