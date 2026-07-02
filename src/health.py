"""
系统健康诊断层。

职责：把"哪个外部依赖/组件出了问题、如何修复"结构化地记录进 state["health"]，
供 main.py 推送 Bark 告警、供 dashboard 在网页显眼处展示。

覆盖范围与根本限制：
  - 组件级故障（系统仍在运行，但某依赖失败）：理杏仁、且慢、引擎、Bark
    → 运行中的代码可检测并推送（Bark 自身故障只能在网页显示）。
  - 整个系统停摆（cron-job.org 停、GitHub 挂、网络断）：无代码运行，发不出 Bark
    → 只能由网页端基于 data.json 的 last_update 时间戳检测（见 index.html）。
    这里保留 scheduler 组件定义，仅用于网页文案与修复指引。
"""
from datetime import datetime, timezone

# 组件注册表：name=展示名，critical=是否影响买卖信号，fix=具体修复方法
COMPONENTS = {
    "lixinger": {
        "name": "理杏仁数据源（PB/PE分位·收盘价）",
        "critical": True,
        "fix": "① 到 GitHub 仓库 Settings → Secrets and variables → Actions 更新 LIXINGER_TOKEN；"
               "② 登录理杏仁确认订阅未到期、API 额度未用尽。这是买卖信号的命根子，修复前引擎拒绝运行。",
    },
    "qieman": {
        "name": "且慢温度计（沪深300/500温度）",
        "critical": False,
        "fix": "更新 QIEMAN_KEY（且慢温度计 MCP 的 x-api-key）。温度仅用于展示，不影响买卖信号，可从容处理。",
    },
    "bark": {
        "name": "Bark 推送通道",
        "critical": False,
        "fix": "检查 BARK_KEY 是否被重置、Bark App 是否欠费/卸载。此项异常时你可能收不到任何手机推送——"
               "请以本仪表盘为准，并尽快修复。",
    },
    "engine": {
        "name": "策略引擎",
        "critical": True,
        "fix": "打开 GitHub 仓库 Actions 页查看最近一次运行日志定位异常堆栈；常见原因是数据格式变化或依赖库更新。",
    },
    "scheduler": {
        "name": "定时器 / 运行链路（cron-job.org + GitHub Actions）",
        "critical": True,
        "fix": "① 登录 cron-job.org 确认定时任务在运行、未被暂停；"
               "② 到 GitHub 仓库 Actions 页手动 Run workflow 验证能否跑通；"
               "③ 确认 GitHub 账号 / 仓库 / Pages 正常。",
    },
}

_VALID = ("ok", "warn", "down")


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def blank():
    return {"updated_at": None, "components": {}}


def mark(state: dict, key: str, status: str, detail: str = "") -> dict:
    """
    记录组件状态。status ∈ {ok, warn, down}。
    保留 last_ok（上次正常日期）与 alerted_date（上次告警日期，用于去重）。
    若从 down/warn 恢复到 ok，置 just_recovered=True（供发送恢复通知后清除）。
    """
    if status not in _VALID:
        raise ValueError(f"未知状态: {status}")
    meta = COMPONENTS.get(key, {})
    h = state.setdefault("health", blank())
    comps = h.setdefault("components", {})
    prev = comps.get(key, {})
    prev_status = prev.get("status")

    comp = {
        "status": status,
        "detail": detail,
        "name": meta.get("name", key),
        "fix": meta.get("fix", ""),
        "critical": meta.get("critical", False),
        "last_ok": _today() if status == "ok" else prev.get("last_ok"),
        "alerted_date": prev.get("alerted_date", ""),
        "just_recovered": bool(prev_status in ("down", "warn") and status == "ok"),
    }
    comps[key] = comp
    h["updated_at"] = _now_str()
    return comp


def overall(state: dict) -> str:
    comps = state.get("health", {}).get("components", {})
    statuses = [c.get("status") for c in comps.values()]
    if "down" in statuses:
        return "down"
    if "warn" in statuses:
        return "warn"
    return "ok"


def pending_alerts(state: dict):
    """今日尚未告警过的 down/warn 组件（每组件每天最多一条，避免刷屏）。"""
    comps = state.get("health", {}).get("components", {})
    return [(k, c) for k, c in comps.items()
            if c.get("status") in ("down", "warn") and c.get("alerted_date") != _today()]


def pending_recoveries(state: dict):
    """本次运行中刚从故障恢复的组件。"""
    comps = state.get("health", {}).get("components", {})
    return [(k, c) for k, c in comps.items() if c.get("just_recovered")]


def mark_alerted(state: dict, key: str):
    comps = state.get("health", {}).get("components", {})
    if key in comps:
        comps[key]["alerted_date"] = _today()


def clear_recovered(state: dict, key: str):
    comps = state.get("health", {}).get("components", {})
    if key in comps:
        comps[key]["just_recovered"] = False


def classify_fetch_error(e) -> str:
    """把数据源异常翻译成人话，尽量指向 token / 额度 / 网络。"""
    s = str(e)
    low = s.lower()
    if "401" in s or "unauthorized" in low or ("token" in low and "invalid" in low):
        return "疑似 token 失效或未授权（HTTP 401）"
    if "403" in s or "forbidden" in low:
        return "疑似无权限或订阅到期（HTTP 403）"
    if "429" in s or "quota" in low or "rate limit" in low or "too many" in low:
        return "疑似 API 调用额度用尽（HTTP 429）"
    if "timeout" in low or "timed out" in low:
        return "网络超时（可能临时波动，已自动重试仍失败）"
    if "connection" in low or "resolve" in low or "network" in low:
        return "网络连接失败（DNS/连通性问题）"
    if "message" in low or "code" in low:
        return f"接口返回异常：{s[:100]}"
    return f"数据拉取失败：{s[:100]}"
