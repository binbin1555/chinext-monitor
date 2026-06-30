#!/usr/bin/env python3
"""
数据探针：测试理杏仁 API 和且慢温度计 MCP API 的连通性与字段结构。
运行：python src/probe.py
"""
import os
import json
import requests
from datetime import date, timedelta

LIXINGER_TOKEN = os.environ.get("LIXINGER_TOKEN", "")
QIEMAN_KEY = os.environ.get("QIEMAN_KEY", "")


def probe_lixinger():
    print("=" * 60)
    print("探针 1：理杏仁 API")
    print("=" * 60)

    end = date.today().strftime("%Y-%m-%d")
    start = (date.today() - timedelta(days=10)).strftime("%Y-%m-%d")

    # K线接口
    url_k = "https://open.lixinger.com/api/cn/index/candlestick"
    body_k = {
        "token": LIXINGER_TOKEN,
        "startDate": start,
        "endDate": end,
        "stockCodes": ["399006", "000300"],
        "type": "normal",
    }
    try:
        r = requests.post(url_k, json=body_k, timeout=30)
        print(f"[K线] 状态码: {r.status_code}")
        print(f"[K线] 响应（前2000字符）:\n{json.dumps(r.json(), ensure_ascii=False, indent=2)[:2000]}")
    except Exception as e:
        print(f"[K线] 异常: {e}")

    print()

    # 基本面接口
    url_f = "https://open.lixinger.com/api/cn/index/fundamental"
    body_f = {
        "token": LIXINGER_TOKEN,
        "startDate": start,
        "endDate": end,
        "stockCodes": ["399006"],
        "metricsList": ["pe_ttm.mcw", "pb.mcw"],
    }
    try:
        r2 = requests.post(url_f, json=body_f, timeout=30)
        print(f"[基本面] 状态码: {r2.status_code}")
        print(f"[基本面] 响应（前2000字符）:\n{json.dumps(r2.json(), ensure_ascii=False, indent=2)[:2000]}")
    except Exception as e:
        print(f"[基本面] 异常: {e}")


def probe_qieman():
    print()
    print("=" * 60)
    print("探针 2：且慢温度计 MCP API")
    print("=" * 60)

    url = "https://stargate.yingmi.com/mcp/v2"
    headers = {
        "x-api-key": QIEMAN_KEY,
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }

    # Step 1: initialize
    init_body = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"sampling": {}},
        },
        "id": 1,
    }
    try:
        print(">>> 发送 initialize 握手...")
        r = requests.post(url, json=init_body, headers=headers, timeout=30)
        print(f"[init] 状态码: {r.status_code}")
        print(f"[init] 响应头: {dict(r.headers)}")
        print(f"[init] 响应体: {r.text[:800]}")
    except Exception as e:
        print(f"[init] 异常: {e}")

    print()

    # Step 2: tools/call
    call_body = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "GetLatestQuotations",
            "arguments": {"calDate": date.today().strftime("%Y-%m-%d")},
        },
        "id": 2,
    }
    try:
        print(">>> 发送 tools/call GetLatestQuotations...")
        r2 = requests.post(url, json=call_body, headers=headers, timeout=30, stream=True)
        print(f"[call] 状态码: {r2.status_code}")
        print(f"[call] Content-Type: {r2.headers.get('Content-Type', '?')}")

        content_type = r2.headers.get("Content-Type", "")
        if "event-stream" in content_type:
            print("[call] 检测到 SSE 事件流，逐行解析：")
            for raw_line in r2.iter_lines():
                if raw_line:
                    line = raw_line if isinstance(raw_line, str) else raw_line.decode("utf-8")
                    print(f"  SSE行: {line[:400]}")
        else:
            print(f"[call] 响应体: {r2.text[:2000]}")
    except Exception as e:
        print(f"[call] 异常: {e}")


if __name__ == "__main__":
    if not LIXINGER_TOKEN:
        print("警告：LIXINGER_TOKEN 未设置，理杏仁接口会鉴权失败")
    if not QIEMAN_KEY:
        print("警告：QIEMAN_KEY 未设置，且慢接口会鉴权失败")
    probe_lixinger()
    probe_qieman()
