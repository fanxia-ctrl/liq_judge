#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""强平 judge 命令行。

    python3 cli.py serve [--port 8788]           起异步服务（服务器上用 bash serve.sh）
    python3 cli.py run turns.jsonl -o out/       本地直接判一批，不经过服务
    python3 cli.py extract <replay_run_dir> --cases <cases.jsonl> [--gold <gold.jsonl>] -o turns.jsonl
                                                 把 liq_replay.py 的回放结果整理成 judge 输入
    python3 cli.py extract-dify --request req.json --events events.jsonl -o turn.json
                                                 Dify 的原始请求 + 事件流 → judge 的一轮
    python3 cli.py selftest                      不打模型，跑通输入校验、判定、投票、报告
    python3 cli.py probe                         真调一次模型端点
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import judge  # noqa: E402
import service  # noqa: E402


def client(kind: str):
    judge.load_env()
    return judge.Mock() if kind == "mock" else judge.GLM()


def read_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def cmd_serve(a) -> int:
    keys = service.parse_keys(os.environ.get("LIQ_JUDGE_KEYS", ""))
    return service.serve(a.host, a.port, Path(a.data), client(a.llm), keys, a.workers)


def cmd_run(a) -> int:
    turns, warnings = service.parse_turns(read_jsonl(Path(a.turns)))
    for w in warnings:
        print("提示：", w)
    llm = client(a.llm)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    cache = judge.Cache(out / "cache.jsonl")
    with ThreadPoolExecutor(a.workers) as p:
        rows = list(p.map(lambda t: judge.judge_turn(t, llm, cache, a.samples, a.tool_name), turns))
    (out / "results.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    meta = {"summary": judge.summarize(rows), "rubric": judge.rubric_fingerprint(), "endpoint": llm.name}
    job = {"id": out.name, "options": json.dumps({"name": a.name, "samples": a.samples, "tool_name": a.tool_name})}
    (out / "report.md").write_text(service.report_md(job, turns, rows, meta), encoding="utf-8")
    print(json.dumps(meta["summary"], ensure_ascii=False))
    print("→", out / "results.jsonl", "|", out / "report.md")
    return 0


def _tool_output(raw: str) -> str:
    """Dify agent_log 里的工具返回形如 tool response: VariableMessage(variable_name='result', variable_value='…')"""
    vals = re.findall(r"variable_name='(\w+)', variable_value=(?:'|\")(.*?)(?:'|\"), stream=False", raw or "", re.S)
    if vals:
        main = [v for k, v in vals if k == "result"] or [v for _, v in vals]
        err = [v for k, v in vals if k == "error" and v]
        return ((err[0] if err and not main[0] else main[0]) or "").replace("\\n", "\n")
    return raw or ""


def cmd_extract(a) -> int:
    recs = read_jsonl(Path(a.run) / "records.jsonl")
    cases = {r["uid"]: r for r in read_jsonl(Path(a.cases))}
    gold = {r["uid"]: r for r in read_jsonl(Path(a.gold))} if a.gold else {}
    out = []
    for r in recs:
        c = cases.get(r["uid"], {})
        calls = []
        for t in r.get("tool_calls") or []:
            calls.append({"name": t.get("tool"), "input": t.get("input"), "output": _tool_output(t.get("response") or ""),
                          **({"error": t["error"]} if t.get("error") else {})})
        turn = {"case_id": r.get("case_id") or r["uid"], "query": r["query"], "answer": r.get("answer") or r.get("err") or "",
                "history": [{"role": m["role"] if m["role"] == "user" else "assistant", "content": m["content"],
                             **({"time": m["time"]} if m.get("time") else {})} for m in c.get("history", [])],
                "tool_calls": calls, "route": (r.get("router") or "").replace('{"topic":"', "").rstrip('"}')}
        if r["uid"] in gold:
            turn["truth"] = {"facts": gold[r["uid"]].get("facts", []), "orders": gold[r["uid"]].get("orders", "")}
        out.append(turn)
    Path(a.out).write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in out), encoding="utf-8")
    print(f"{len(out)} 轮 → {a.out}")
    return 0


def cmd_extract_dify(a) -> int:
    """Dify /v1/chat-messages 的一次调用 → judge 的一轮。
    输入两个文件：请求体（含 inputs.conversation_history）和原始事件流（一行一个 SSE data 的 JSON）。"""
    req = json.loads(Path(a.request).read_text(encoding="utf-8"))
    events = read_jsonl(Path(a.events))
    answer, calls, route = "", [], ""
    for ev in events:
        e = ev.get("event")
        if e == "message":
            answer += ev.get("answer") or ""
        elif e == "agent_log":
            d = ev.get("data") or {}
            if str(d.get("label", "")).startswith("CALL ") and d.get("status") in ("success", "error"):
                out = (d.get("data") or {}).get("output") or {}
                calls.append({"name": out.get("tool_call_name") or d["label"][5:],
                              "input": out.get("tool_call_input"),
                              "output": _tool_output(str(out.get("tool_response") or "")),
                              **({"error": d["error"]} if d.get("error") else {})})
        elif e == "node_finished" and (ev.get("data") or {}).get("title") == "ROUTER":
            route = str(((ev["data"].get("outputs") or {}).get("text")) or "")
    hist_raw = (req.get("inputs") or {}).get("conversation_history") or ""
    try:
        hist = json.loads(hist_raw) if hist_raw else []
    except ValueError:
        hist = [{"role": "user", "content": hist_raw}]
    query = req.get("query") or ""
    asked_at = ""
    if hist and hist[-1].get("role") == "user" and hist[-1].get("content") == query:
        last = hist.pop()                      # history parser 会把最后一条用户消息当作当前问题，这里同样剥掉
        if a.asked_at_from_history and last.get("ts"):
            from datetime import datetime
            asked_at = datetime.fromtimestamp(int(last["ts"])).strftime("%Y-%m-%d %H:%M:%S")
    turn = {"case_id": a.case_id or req.get("user") or "t0001", "query": query, "answer": answer.strip(),
            "history": [{"role": m.get("role"), "content": m.get("content")} for m in hist],
            "tool_calls": calls, "route": route.replace('{"topic":"', "").rstrip('"}')}
    if asked_at:
        turn["asked_at"] = asked_at
    if a.gold:
        g = {r["uid"]: r for r in read_jsonl(Path(a.gold))}.get(str(req.get("user")))
        if g:
            turn["truth"] = {"facts": g.get("facts", []), "orders": g.get("orders", "")}
    Path(a.out).write_text(json.dumps(turn, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"→ {a.out}  工具调用 {len(calls)} 次，回答 {len(turn['answer'])} 字，历史 {len(turn['history'])} 条")
    return 0


def cmd_selftest(a) -> int:
    ok = True

    def check(name, cond):
        nonlocal ok
        ok &= bool(cond)
        print(("✓ " if cond else "✗ ") + name)

    turns = [
        {"case_id": "a", "query": "为什么给我强平了", "answer": "您这笔 ETH 空单在 2754.15 触发强平。",
         "history": "用户：我的 ETH 空单怎么没了", "truth": "ETH 空 2754.15",
         "tool_calls": [{"name": "analyze_deepcoin_liquidation", "input": {"uid": 1}, "output": "理论强平价 2754.2"}]},
        {"case_id": "b", "query": "为什么没有强平", "answer": "强平记录暂时无法查询，请稍后再试。",
         "tool_calls": [{"name": "analyze_deepcoin_liquidation_v2", "output": "[查询失败] 服务超时"}]},
        {"case_id": "c", "query": "显示亏 400 最后变 500", "answer": "这是手续费造成的。", "tool_calls": []},
        {"case_id": "d", "query": "为什么强平", "answer": "您的仓位在 2700 被强平。"},
        {"case_id": "e", "query": "为什么强平", "answer": "您的仓位在 2700 被强平。", "evidence_text": "强平价 2700"},
        {"case_id": "f", "query": "为什么强平", "answer": "……",
         "tool_calls": [{"name": "liquidation_tool", "output": "强平价 2700"}]},
    ]
    parsed, warnings = service.parse_turns(turns)
    check("输入校验通过、缺字段给出提示", len(parsed) == 6 and any("evidence_text" in w for w in warnings))
    for bad, msg in [([{"answer": "x"}], "缺 query"), ([{"query": "x"}], "缺 answer"),
                     ([{"query": "x", "answer": "y", "case_id": "d"}] * 2, "case_id 重复")]:
        try:
            service.parse_turns(bad)
            check(f"拒绝{msg}", False)
        except service.BadRequest:
            check(f"拒绝{msg}", True)
    check("工具状态：正常 / 失败 / 未调用 / 未提供 / 材料当工具返回 / 工具名不匹配",
          [judge.tool_status(t)[0] for t in turns] == ["正常", "失败", "未调用", "未提供", "正常", "未调用"])
    check("工具名可指定", judge.tool_status(turns[5], "liquidation_tool")[0] == "正常")
    check("同义词归一", judge.validate({"event_match": "匹配", "grounding": "忠实", "failure_handling": "不适用",
                                       "answers_question": "是", "verdict": "通过", "blame": "无"})["event_match"] == "一致")
    check("投票打平取更严重", judge._vote(["通过", "不通过"], "verdict") == "不通过")
    check("推理泄漏时仍能取到 JSON", judge.parse_json('想一想 {"a":1} </think>{"a":2}') == {"a": 2})
    rows = [judge.judge_turn(t, judge.Mock(), judge.Cache(None), 3) for t in parsed]
    check("mock 判定结果", [r["verdict"] for r in rows[:3]] == ["通过", "通过", "部分"] and rows[1]["failure_handling"] == "如实告知")
    check("未调用 → 责任层路由；未提供 → 不判路由、记 undecidable",
          rows[2]["blame"] == "路由" and rows[3]["blame"] != "路由" and rows[3]["undecidable"] == ["event_match", "failure_handling"])
    md = service.report_md({"id": "selftest", "options": json.dumps({"name": "自检", "samples": 3})}, parsed, rows,
                           {"summary": judge.summarize(rows), "rubric": judge.rubric_fingerprint(), "endpoint": "mock"})
    check("报告能生成", "## 汇总" in md and "## 逐轮" in md)
    print("全部通过" if ok else "有失败项")
    return 0 if ok else 1


def cmd_probe(a) -> int:
    llm = client("glm")
    try:
        raw = llm.complete("只回复 ok", "ok", 0.0)
        print("模型端点可用：", llm.name, "|", raw.strip()[:60])
        return 0
    except Exception as e:
        print("模型端点不可用：", e)
        return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="强平 judge")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8788)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--data", default=str(HERE / "data"))
    p.add_argument("--llm", default="glm", choices=["glm", "mock"])
    p.set_defaults(fn=cmd_serve)
    p = sub.add_parser("run")
    p.add_argument("turns")
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--samples", type=int, default=3)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--name", default="")
    p.add_argument("--tool-name", default=judge.LIQ_TOOL, help="强平工具名前缀")
    p.add_argument("--llm", default="glm", choices=["glm", "mock"])
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("extract")
    p.add_argument("run")
    p.add_argument("--cases", required=True)
    p.add_argument("--gold")
    p.add_argument("-o", "--out", required=True)
    p.set_defaults(fn=cmd_extract)
    p = sub.add_parser("extract-dify", help="Dify 原始请求 + 事件流 → judge 的一轮")
    p.add_argument("--request", required=True)
    p.add_argument("--events", required=True)
    p.add_argument("--gold")
    p.add_argument("--case-id", default="")
    p.add_argument("--no-asked-at", dest="asked_at_from_history", action="store_false",
                   help="不从历史最后一条消息的时间推 asked_at（回放数据的时间是平移过的，要加这个）")
    p.add_argument("-o", "--out", required=True)
    p.set_defaults(fn=cmd_extract_dify)
    sub.add_parser("selftest").set_defaults(fn=cmd_selftest)
    sub.add_parser("probe").set_defaults(fn=cmd_probe)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
