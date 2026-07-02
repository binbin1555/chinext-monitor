"""
Bark iOS 推送层：信号通知、预警、每日日报。
"""
import os
import logging
import requests
from datetime import datetime
from urllib.parse import quote

logger = logging.getLogger(__name__)

BARK_BASE = "https://api.day.app"
DASHBOARD_URL = os.environ.get(
    "DASHBOARD_URL",
    "https://your-username.github.io/chinext-monitor/"
)

SIGNAL_TYPE_LABELS = {
    "T1":              "🟢 T1 买入信号",
    "T2":              "🟢 T2 买入信号",
    "T3":              "🟢 T3 满仓信号",
    "weekly_6":        "🟢 周定投（6份）",
    "weekly_3":        "🟢 周定投（3份）",
    "rightside":       "🟢 右侧补仓信号",
    "enter_observation": "🟡 进入止盈观察期",
    "reduce":          "🟠 减仓50%信号",
    "exit":            "🔴 全部止盈信号",
}


class BarkNotifier:
    def __init__(self, bark_key: str):
        self.bark_key = bark_key
        self.enabled = bool(bark_key and bark_key.strip())
        # 发送结果追踪：供 main.py 判断 Bark 通道本身是否健康
        self.attempted = 0        # 本次运行尝试发送次数
        self.failures  = 0        # 其中失败次数
        self.last_error = ""      # 最近一次失败原因

    def _send(self, title: str, body: str, level: str = "active",
              group: str = "创业板择时", url: str = None):
        if not self.enabled:
            logger.info(f"[Bark 未配置] {title}: {body}")
            return

        title_enc = quote(title, safe="")
        body_enc  = quote(body,  safe="")
        endpoint  = f"{BARK_BASE}/{self.bark_key}/{title_enc}/{body_enc}"
        params = {"group": group, "level": level}
        if url:
            params["url"] = url

        self.attempted += 1
        try:
            r = requests.get(endpoint, params=params, timeout=15)
            if r.status_code != 200:
                self.failures += 1
                self.last_error = f"HTTP {r.status_code}: {r.text[:120]}"
                logger.warning(f"Bark 推送失败 {r.status_code}: {r.text[:200]}")
            else:
                logger.info(f"Bark 推送成功: {title}")
        except Exception as e:
            self.failures += 1
            self.last_error = str(e)[:120]
            logger.error(f"Bark 推送异常: {e}")

    def bark_healthy(self):
        """本次运行 Bark 通道是否正常。None=未尝试发送，无法判断。"""
        if not self.enabled or self.attempted == 0:
            return None
        return self.failures == 0

    def send_signal(self, signal: dict, today_str: str,
                    total_bought: int, total_fen: int = 150):
        """推送买/卖信号。"""
        sig_type = signal.get("type", "")
        label    = SIGNAL_TYPE_LABELS.get(sig_type, f"📊 {sig_type}")
        fen      = signal.get("fen", 0)
        price    = signal.get("price")
        pb_pct   = signal.get("pb_pct")
        reason   = signal.get("reason", "")
        level    = signal.get("level", "active")

        body_lines = [reason]
        if price:
            body_lines.append(f"当前点位: {price:.0f}")
        if pb_pct is not None:
            body_lines.append(f"PB近10年分位: {pb_pct*100:.1f}%")
        if fen > 0:
            body_lines.append(f"操作份数: {fen}份（已买{total_bought}/{total_fen}份）")
        body_lines.append(f"日期: {today_str}")

        self._send(
            title=f"{label}（{today_str}）",
            body="\n".join(body_lines),
            level=level,
            url=DASHBOARD_URL,
        )

    def send_warnings(self, warnings: list, today_str: str):
        """推送临近预警（合并为一条）。"""
        if not warnings:
            return
        self._send(
            title=f"⚠️ 临近预警（{today_str}）",
            body="\n".join(warnings),
            level="active",
            url=DASHBOARD_URL,
        )

    def send_daily_report(self, today_str: str, metrics: dict,
                          state: dict, ledger_state: dict,
                          row_data: dict, warnings: list):
        """每日日报：无论有无信号都发送，是系统心跳。"""
        phase_map = {
            "waiting": "空仓等待",
            "holding": "建仓/持有中",
        }
        phase = phase_map.get(state.get("phase", ""), state.get("phase", ""))

        close   = row_data.get("close")
        pb_pct  = row_data.get("pb_pct10y")
        pe_pct  = row_data.get("pe_pct10y")
        ma120   = row_data.get("ma120")
        t300    = row_data.get("temp_300")
        t500    = row_data.get("temp_500")

        fp = None
        if ledger_state.get("weighted_avg") and close:
            fp = close / ledger_state["weighted_avg"] - 1

        lines = [
            f"📊 创业板监测日报 {today_str}",
            f"阶段: {phase}",
            "─" * 24,
        ]
        if close:
            ma120_str = f"{ma120:.0f}" if ma120 else "N/A"
            lines.append(f"创业板: {close:.0f}  MA120: {ma120_str}")
        if pb_pct is not None:
            pe_str = f"{pe_pct*100:.1f}%" if pe_pct is not None else "N/A"
            lines.append(f"PB分位: {pb_pct*100:.1f}%  PE分位: {pe_str}")
        if t300 is not None:
            t500_str = f"{t500:.1f}" if t500 is not None else "N/A"
            lines.append(f"温度: 沪深300={t300:.1f}  中证500={t500_str}")
        if fp is not None:
            lines.append(f"当前浮盈: {fp*100:.1f}%  持仓: {ledger_state.get('current_fen', 0)}/{150}份")
        if metrics.get("cagr") is not None:
            lines.append(f"CAGR: {metrics['cagr']*100:.1f}%  回撤: {metrics.get('max_drawdown', 0)*100:.1f}%")
        if warnings:
            lines.append("─" * 24)
            lines.extend(warnings)
        if state.get("signals_pending"):
            lines.append("─" * 24)
            lines.append(f"⏳ 有 {len(state['signals_pending'])} 条操作提醒待处理（查看仪表盘）")
        lines.append(f"仪表盘: {DASHBOARD_URL}")

        self._send(
            title=f"📊 日报（{today_str}）",
            body="\n".join(lines),
            level="active",
            url=DASHBOARD_URL,
        )

    def send_error(self, error_msg: str):
        """系统异常告警。"""
        self._send(
            title="🔴 创业板监测系统异常",
            body=error_msg,
            level="timeSensitive",
        )

    def send_health_alert(self, name: str, detail: str, fix: str,
                          today_str: str = ""):
        """
        组件级健康告警：明确告知【哪个组件】出了【什么问题】、【如何修复】。
        每个组件每天最多一条（去重在 main.py 侧控制）。
        """
        date_suffix = f"（{today_str}）" if today_str else ""
        body = f"⚠️ 问题：{detail or '组件不可用'}\n\n🛠️ 解决方法：\n{fix}"
        self._send(
            title=f"🔴 系统组件异常：{name}{date_suffix}",
            body=body,
            level="timeSensitive",
            group="创业板系统健康",
        )

    def send_health_recovery(self, name: str, today_str: str = ""):
        """组件从故障恢复的通知。"""
        date_suffix = f"（{today_str}）" if today_str else ""
        self._send(
            title=f"✅ 系统组件已恢复：{name}{date_suffix}",
            body="该组件已恢复正常，系统监测继续。",
            level="active",
            group="创业板系统健康",
        )

    def send_info(self, title: str, body: str):
        """一般信息通知（如数据延迟提示），不打扰为 timeSensitive。"""
        self._send(title=title, body=body, level="active")
