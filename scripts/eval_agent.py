"""SuperBizAgent 评测脚本

对运行中的 Agent (http://localhost:9900) 做端到端黑盒评测，覆盖：
1. 监控数据真实性：Agent 报告的 CPU/内存使用率 vs 系统真实值 (/proc)
2. Mock 数据源检测：直接调用 monitor MCP 服务的函数，与真实值对比
3. RAG 知识库问答：已索引文档的关键点命中
4. RAG 负例：知识库中没有的内容不应编造
5. 工具调用准确性：当前时间工具 vs 系统时间
6. 多轮会话记忆：同 session 的指代追问
7. 流式接口协议：SSE 事件完整性

用法: .venv/bin/python scripts/eval_agent.py
输出: 控制台表格 + scripts/eval_report_<timestamp>.json
"""

import json
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests

BASE_URL = "http://localhost:9900"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

RESULTS = []


def record(name, passed, detail, latency_s=None, severity="fail"):
    RESULTS.append({
        "case": name,
        "passed": passed,
        "detail": detail,
        "latency_s": latency_s,
        "severity": severity,  # fail=硬失败, warn=软失败
    })
    icon = "✅" if passed else ("⚠️" if severity == "warn" else "❌")
    lat = f" ({latency_s:.1f}s)" if latency_s is not None else ""
    print(f"{icon} {name}{lat}\n   {detail}\n")


# ------------------------------------------------------------
# 系统真实值 (ground truth)
# ------------------------------------------------------------

def real_cpu_usage(percent_interval: float = 1.0) -> float:
    """采样 /proc/stat 两次计算真实 CPU 使用率"""
    def read():
        with open("/proc/stat") as f:
            parts = f.readline().split()[1:]
        vals = [int(x) for x in parts]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        return idle, sum(vals)
    idle1, total1 = read()
    time.sleep(percent_interval)
    idle2, total2 = read()
    dt = total2 - total1
    if dt <= 0:
        return 0.0
    return round(100.0 * (1 - (idle2 - idle1) / dt), 1)


def real_mem_usage() -> float:
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":")
            info[k.strip()] = int(v.strip().split()[0])
    total = info["MemTotal"]
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    return round(100.0 * (1 - avail / total), 1)


def extract_percents(text: str):
    """优先提取带 % 的数值；若回答为纯数字（如 '97.0'）则回退提取独立浮点数"""
    vals = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*%", text)]
    if vals:
        return vals
    stripped = text.strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)", stripped)
    if m:
        return [float(m.group(1))]
    return [float(x) for x in re.findall(r"(?<![\d.:])(\d+(?:\.\d+)?)(?![\d:%])", stripped)]


def extract_time_hhmm(text: str):
    m = re.search(r"(\d{1,2})\s*[:：点]\s*(\d{2})", text)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


# ------------------------------------------------------------
# Agent 调用
# ------------------------------------------------------------

def ask(question: str, session_id: str = None):
    sid = session_id or f"eval-{uuid.uuid4().hex[:8]}"
    t0 = time.time()
    resp = requests.post(
        f"{BASE_URL}/api/chat",
        json={"Id": sid, "Question": question},
        timeout=180,
    )
    lat = time.time() - t0
    resp.raise_for_status()
    data = resp.json().get("data") or {}
    return data.get("answer") or "", lat, sid


def ask_stream(question: str, session_id: str = None):
    sid = session_id or f"eval-{uuid.uuid4().hex[:8]}"
    t0 = time.time()
    with requests.post(
        f"{BASE_URL}/api/chat_stream",
        json={"Id": sid, "Question": question},
        stream=True,
        timeout=180,
    ) as resp:
        resp.raise_for_status()
        events = []
        for line in resp.iter_lines(decode_unicode=True):
            if line and line.startswith("data:"):
                try:
                    events.append(json.loads(line[5:].strip()))
                except json.JSONDecodeError:
                    pass
    return events, time.time() - t0


# ------------------------------------------------------------
# 测试用例
# ------------------------------------------------------------

def case_mock_source_detection():
    """直接调用 monitor MCP 服务函数，对比真实 CPU，证明数据源是否为 mock"""
    sys.path.insert(0, str(PROJECT_ROOT / "mcp_servers"))
    try:
        import monitor_server
        mock1 = monitor_server.query_cpu_metrics("eval-service")
        time.sleep(1)
        mock2 = monitor_server.query_cpu_metrics("eval-service")
        v1 = [p["value"] for p in mock1.get("data_points", mock1.get("logs", []))]
        v2 = [p["value"] for p in mock2.get("data_points", mock2.get("logs", []))]
        truth = real_cpu_usage()
        mock_max = max(v1) if v1 else None
        identical = v1 == v2
        detail = (
            f"monitor_server.query_cpu_metrics 两次调用结果相同={identical}; "
            f"mock 峰值={mock_max}%, 系统真实 CPU={truth}%。"
            f"该函数为随机/脚本化造数，与真实系统无关"
        )
        # 期望: mock 与真实值严重不符 或 两次调用结果不同(随机) -> 证明是 mock
        is_mock = (not identical) or (mock_max is not None and abs(mock_max - truth) > 20)
        record("监控数据源为 mock (根因)", is_mock, detail, severity="warn")
    except Exception as e:
        record("监控数据源为 mock (根因)", False, f"无法直接调用 monitor_server: {e}", severity="warn")


def case_cpu_truthfulness():
    truth = real_cpu_usage()
    answer, lat, _ = ask("这台服务器当前的CPU使用率是多少？请只回答一个百分比数字")
    vals = extract_percents(answer)
    if not vals:
        record("CPU 数据真实性", False, f"回答中无百分比: {answer[:120]}", lat)
        return
    reported = vals[0]
    diff = abs(reported - truth)
    record(
        "CPU 数据真实性",
        diff <= 20,
        f"Agent 报告 {reported}% vs 系统真实 {truth}% (偏差 {diff:.1f} 个百分点); 回答: {answer[:100]}",
        lat,
    )


def case_mem_truthfulness():
    truth = real_mem_usage()
    answer, lat, _ = ask("这台服务器当前的内存使用率是多少？请只回答一个百分比数字")
    vals = extract_percents(answer)
    if not vals:
        record("内存数据真实性", False, f"回答中无百分比: {answer[:120]}", lat)
        return
    reported = vals[0]
    diff = abs(reported - truth)
    record(
        "内存数据真实性",
        diff <= 20,
        f"Agent 报告 {reported}% vs 系统真实 {truth}% (偏差 {diff:.1f} 个百分点); 回答: {answer[:100]}",
        lat,
    )


def case_rag_kb_qa():
    answer, lat, _ = ask("根据知识库文档，CPU使用率过高时应该如何排查？请列出步骤")
    keywords = ["步骤", "进程", "日志"]
    hits = [k for k in keywords if k in answer]
    record(
        "RAG 知识库问答 (已索引文档)",
        len(hits) >= 2 and len(answer) > 100,
        f"关键点命中 {hits} ({len(hits)}/{len(keywords)}), 回答长度 {len(answer)}",
        lat,
    )


def case_rag_negative():
    answer, lat, _ = ask("根据知识库，员工请假审批流程是怎样的？")
    hedges = ["没有", "未找到", "无法", "不包含", "暂无", "不知道", "未涉及"]
    hedged = any(h in answer for h in hedges)
    record(
        "RAG 负例 (不编造知识库内容)",
        hedged,
        f"回答是否声明知识库无此内容: {hedged}; 回答: {answer[:120]}",
        lat,
        severity="warn",
    )


def case_time_tool():
    answer, lat, _ = ask("现在几点了？请用 HH:MM 格式回答")
    hm = extract_time_hhmm(answer)
    if not hm:
        record("时间工具准确性", False, f"回答中无时间: {answer[:120]}", lat)
        return
    now = datetime.now()
    diff_min = abs((hm[0] * 60 + hm[1]) - (now.hour * 60 + now.minute))
    diff_min = min(diff_min, 1440 - diff_min)
    record(
        "时间工具准确性",
        diff_min <= 10,
        f"Agent 回答 {hm[0]:02d}:{hm[1]:02d} vs 系统时间 {now:%H:%M} (偏差 {diff_min} 分钟)",
        lat,
    )


def case_multiturn():
    sid = f"eval-mt-{uuid.uuid4().hex[:8]}"
    a1, lat1, _ = ask("根据知识库，CPU使用率过高的排查步骤有哪些？", session_id=sid)
    a2, lat2, _ = ask("其中第一步具体要做什么？", session_id=sid)
    ok = len(a2) > 30 and any(k in a2 for k in ["步骤", "第一", "首先", "1", "一"])
    record(
        "多轮会话记忆 (指代追问)",
        ok,
        f"追问回答: {a2[:120]}",
        lat1 + lat2,
    )


def case_stream_protocol():
    events, lat = ask_stream("用一句话解释什么是RAG")
    contents = [e for e in events if e.get("type") == "content"]
    dones = [e for e in events if e.get("type") in ("done", "complete")]
    errors = [e for e in events if e.get("type") == "error"]
    text = "".join(e.get("data") or "" for e in contents)
    record(
        "流式接口 SSE 协议完整性",
        len(contents) > 0 and len(dones) >= 1 and not errors and len(text) > 10,
        f"content 事件 {len(contents)} 个, done 事件 {len(dones)} 个, error {len(errors)} 个, 拼接文本 {len(text)} 字",
        lat,
    )


def main():
    print(f"=== SuperBizAgent 评测开始 {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    print(f"真实系统基线: CPU={real_cpu_usage()}%, MEM={real_mem_usage()}%\n")

    case_mock_source_detection()
    case_cpu_truthfulness()
    case_mem_truthfulness()
    case_rag_kb_qa()
    case_rag_negative()
    case_time_tool()
    case_multiturn()
    case_stream_protocol()

    hard = [r for r in RESULTS if not r["passed"] and r["severity"] == "fail"]
    warn = [r for r in RESULTS if not r["passed"] and r["severity"] == "warn"]
    passed = [r for r in RESULTS if r["passed"]]
    print("=" * 60)
    print(f"总计 {len(RESULTS)} 例: 通过 {len(passed)}, 失败 {len(hard)}, 警告 {len(warn)}")
    for r in RESULTS:
        icon = "✅" if r["passed"] else ("⚠️" if r["severity"] == "warn" else "❌")
        print(f"  {icon} {r['case']}" + (f" [{r['latency_s']:.1f}s]" if r["latency_s"] else ""))

    out = PROJECT_ROOT / "scripts" / f"eval_report_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入: {out}")
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
