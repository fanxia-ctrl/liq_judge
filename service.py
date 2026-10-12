# -*- coding: utf-8 -*-
"""强平 judge 异步服务。标准库实现，接口与文本 judge 服务一致。

    POST /v1/jobs                    提交一批轮次 → 202 + 任务 id
    GET  /v1/jobs                    我提交过的任务
    GET  /v1/jobs/{id}               状态、进度、排队位置、汇总
    GET  /v1/jobs/{id}/results       逐轮结果（?format=jsonl 逐行）
    GET  /v1/jobs/{id}/report        汇总报告 report.md
    POST /v1/jobs/{id}/cancel        取消
    GET  /v1/rubric                  判定标准 + 判据指纹
    GET  /healthz[?upstream=1]       存活；带 upstream=1 真打一次模型
"""
from __future__ import annotations

import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import judge

MAX_BODY = 64 * 1024 * 1024
MAX_TURNS = 2000
JOB_PATH = re.compile(r"^/v1/jobs/([\w-]+)(?:/(results|report|cancel))?$")


# ── 输入校验 ──────────────────────────────────────────────────────────
class BadRequest(ValueError):
    pass


def parse_options(raw: dict) -> dict:
    out = {"name": str(raw.get("name") or "")[:80], "samples": 3,
           "tool_name": str(raw.get("tool_name") or judge.LIQ_TOOL).strip()[:80]}
    if raw.get("samples") not in (None, ""):
        try:
            n = int(raw["samples"])
        except (TypeError, ValueError):
            raise BadRequest("samples 必须是 1～5 的整数")
        if not 1 <= n <= 5:
            raise BadRequest("samples 必须是 1～5 的整数")
        out["samples"] = n
    return out


def parse_turns(raw) -> tuple[list[dict], list[str]]:
    if not isinstance(raw, list) or not raw:
        raise BadRequest("没有轮次")
    if len(raw) > MAX_TURNS:
        raise BadRequest(f"一次最多 {MAX_TURNS} 轮，这次 {len(raw)} 轮")
    turns, seen, miss = [], set(), {"history": 0, "tool": 0, "truth": 0, "asked_at": 0}
    for i, t in enumerate(raw, 1):
        if not isinstance(t, dict):
            raise BadRequest(f"第 {i} 轮不是 JSON 对象")
        if not str(t.get("query") or "").strip():
            raise BadRequest(f"第 {i} 轮缺少 query")
        if not str(t.get("answer") or "").strip():
            raise BadRequest(f"第 {i} 轮缺少 answer")
        cid = str(t.get("case_id") or f"t{i:04d}")
        if cid in seen:
            raise BadRequest(f"第 {i} 轮的 case_id {cid} 重复")
        seen.add(cid)
        tc = t.get("tool_calls")
        if tc is not None and not isinstance(tc, list):
            raise BadRequest(f"第 {i} 轮的 tool_calls 必须是数组")
        if tc is None and not str(t.get("evidence_text") or "").strip():
            miss["tool"] += 1
        for k in ("history", "truth", "asked_at"):
            if not t.get(k):
                miss[k] += 1
        turns.append({**t, "case_id": cid})
    warnings = []
    if miss["tool"]:
        warnings.append(f"{miss['tool']} 轮既没有 tool_calls 也没有 evidence_text：事件是否对得上、工具失败时的处理两项判不了，记在 undecidable 里")
    if miss["history"]:
        warnings.append(f"{miss['history']} 轮没有 history：无法确定用户在问哪一次强平，事件是否对得上会判得偏弱")
    if miss["truth"]:
        warnings.append(f"{miss['truth']} 轮没有 truth：没有订单真值，只能对照工具返回和对话历史，取错事件可能判不出来")
    if miss["asked_at"]:
        warnings.append(f"{miss['asked_at']} 轮没有 asked_at：工具取到提问之后才发生的强平时，只能靠订单真值里的标记识别")
    return turns, warnings


def parse_body(body: bytes, ctype: str, query: dict) -> tuple[list[dict], dict, list[str]]:
    ctype = (ctype or "").split(";")[0].strip().lower()
    text = body.decode("utf-8", "replace")
    if ctype == "application/x-ndjson":
        rows = []
        for n, line in enumerate(text.splitlines(), 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    raise BadRequest(f"第 {n} 行不是合法 JSON")
        opts = {k: v[0] for k, v in query.items()}
    elif ctype == "application/json":
        try:
            d = json.loads(text)
        except ValueError:
            raise BadRequest("请求体不是合法 JSON")
        if not isinstance(d, dict):
            raise BadRequest("请求体应为 {\"turns\": [...], \"options\": {...}}")
        rows, opts = d.get("turns"), {**{k: v[0] for k, v in query.items()}, **(d.get("options") or {})}
    else:
        raise BadRequest("Content-Type 只支持 application/x-ndjson 或 application/json")
    turns, warnings = parse_turns(rows)
    return turns, parse_options(opts), warnings


# ── 存储 ──────────────────────────────────────────────────────────────
class Store:
    def __init__(self, path: Path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, owner TEXT, status TEXT, options TEXT, warnings TEXT,
            total INTEGER, created REAL, started REAL, finished REAL, error TEXT, cancel INTEGER DEFAULT 0, meta TEXT);
        CREATE TABLE IF NOT EXISTS turns(job TEXT, idx INTEGER, turn TEXT, result TEXT, PRIMARY KEY(job, idx));""")

    def q(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def create(self, owner, turns, options, warnings) -> str:
        jid = datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
        with self.lock:
            self.db.execute("BEGIN")
            self.db.execute("INSERT INTO jobs(id,owner,status,options,warnings,total,created,meta) VALUES(?,?,?,?,?,?,?,?)",
                            (jid, owner, "queued", json.dumps(options, ensure_ascii=False),
                             json.dumps(warnings, ensure_ascii=False), len(turns), time.time(), "{}"))
            self.db.executemany("INSERT INTO turns(job,idx,turn) VALUES(?,?,?)",
                                [(jid, i, json.dumps(t, ensure_ascii=False)) for i, t in enumerate(turns)])
            self.db.execute("COMMIT")
        return jid

    def job(self, jid):
        r = self.q("SELECT * FROM jobs WHERE id=?", (jid,))
        return dict(r[0]) if r else None

    def jobs(self, owner, limit=20):
        return [dict(r) for r in self.q("SELECT * FROM jobs WHERE owner=? ORDER BY created DESC LIMIT ?", (owner, limit))]

    def done_count(self, jid):
        return self.q("SELECT COUNT(*) n FROM turns WHERE job=? AND result IS NOT NULL", (jid,))[0]["n"]

    def queue_position(self, jid):
        j = self.job(jid)
        if not j or j["status"] != "queued":
            return None
        return self.q("SELECT COUNT(*) n FROM jobs WHERE status='queued' AND created<?", (j["created"],))[0]["n"]

    def next_queued(self):
        r = self.q("SELECT id FROM jobs WHERE status='queued' ORDER BY created LIMIT 1")
        return r[0]["id"] if r else None

    def pending(self, jid):
        return [(r["idx"], json.loads(r["turn"])) for r in
                self.q("SELECT idx, turn FROM turns WHERE job=? AND result IS NULL ORDER BY idx", (jid,))]

    def results(self, jid):
        return [json.loads(r["result"]) if r["result"] else None for r in
                self.q("SELECT result FROM turns WHERE job=? ORDER BY idx", (jid,))]

    def turns(self, jid):
        return [json.loads(r["turn"]) for r in self.q("SELECT turn FROM turns WHERE job=? ORDER BY idx", (jid,))]

    def save(self, jid, idx, row):
        self.q("UPDATE turns SET result=? WHERE job=? AND idx=?", (json.dumps(row, ensure_ascii=False), jid, idx))

    def set(self, jid, **kw):
        cols = ", ".join(f"{k}=?" for k in kw)
        self.q(f"UPDATE jobs SET {cols} WHERE id=?", (*kw.values(), jid))

    def requeue_interrupted(self):
        self.q("UPDATE jobs SET status='queued' WHERE status='running'")


# ── 后台执行 ──────────────────────────────────────────────────────────
class Runner:
    """调度线程按提交顺序取任务；任务内按轮并行，并行度是全局上限（上游只有一台 vLLM）。"""

    def __init__(self, store: Store, llm, data_dir: Path, workers: int):
        self.store, self.llm, self.data_dir = store, llm, data_dir
        self.pool = ThreadPoolExecutor(workers)
        self.cache = judge.Cache(data_dir / "cache.jsonl")
        self.wake = threading.Event()
        self.running: str | None = None

    def start(self):
        self.store.requeue_interrupted()
        threading.Thread(target=self._loop, daemon=True).start()

    def poke(self):
        self.wake.set()

    def _loop(self):
        while True:
            jid = self.store.next_queued()
            if not jid:
                self.wake.wait(5)
                self.wake.clear()
                continue
            try:
                self._run(jid)
            except Exception:
                self.store.set(jid, status="failed", finished=time.time(), error=traceback.format_exc()[-800:])
            finally:
                self.running = None

    def _run(self, jid):
        job = self.store.job(jid)
        opts = json.loads(job["options"])
        self.running = jid
        self.store.set(jid, status="running", started=job["started"] or time.time(),
                       meta=json.dumps({"rubric": judge.rubric_fingerprint(), "endpoint": self.llm.name}, ensure_ascii=False))
        futs = {self.pool.submit(judge.judge_turn, t, self.llm, self.cache, opts["samples"],
                                 opts.get("tool_name") or judge.LIQ_TOOL): i
                for i, t in self.store.pending(jid)}
        for f in as_completed(futs):
            self.store.save(jid, futs[f], f.result())
            if self.store.job(jid)["cancel"]:
                for g in futs:
                    g.cancel()
                self.store.set(jid, status="cancelled", finished=time.time())
                return
        rows = self.store.results(jid)
        meta = {"summary": judge.summarize(rows), "rubric": judge.rubric_fingerprint(), "endpoint": self.llm.name}
        self.store.set(jid, status="done", finished=time.time(), meta=json.dumps(meta, ensure_ascii=False))
        out = self.data_dir / "jobs" / jid
        out.mkdir(parents=True, exist_ok=True)
        (out / "results.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        (out / "report.md").write_text(report_md(job, self.store.turns(jid), rows, meta), encoding="utf-8")


# ── 报告 ──────────────────────────────────────────────────────────────
def report_md(job: dict, turns: list[dict], rows: list[dict | None], meta: dict) -> str:
    opts = json.loads(job["options"])
    s = meta["summary"]
    lines = [f"# 强平 judge 报告{('：' + opts['name']) if opts.get('name') else ''}", "",
             f"任务 `{job['id']}` · {len(turns)} 轮 · 每轮判定 {opts['samples']} 次 · 判据 `{meta['rubric']}` · 模型 `{meta['endpoint']}`", "",
             *([f"{n} 轮缺材料，部分项判不了，见各轮 undecidable", ""] if (n := sum(1 for r in rows if r and r.get('undecidable'))) else []),
             "## 汇总", "", "| 项 | 分布 |", "|---|---|"]
    names = {"verdict": "总判定", "blame": "责任层", "grounding": "回答是否有出处", "event_match": "事件是否对得上",
             "failure_handling": "工具失败时的处理", "answers_question": "是否回应具体疑问"}
    for k, n in names.items():
        lines.append(f"| {n} | {'，'.join(f'{v} {c}' for v, c in s.get(k, {}).items()) or '—'} |")
    lines += ["", "## 逐轮", "", "| case_id | 问题 | 判定 | 责任层 | 结论 |", "|---|---|---|---|---|"]
    for t, r in zip(turns, rows):
        r = r or {}
        q = " ".join(str(t.get("query", "")).split())[:30].replace("|", "｜")
        why = " ".join(str(r.get("why", "")).split())[:80].replace("|", "｜")
        lines.append(f"| {t['case_id']} | {q} | {r.get('verdict') or '—'} | {r.get('blame') or '—'} | {why} |")
    bad = [(t, r) for t, r in zip(turns, rows) if r and r.get("verdict") != "通过" and r.get("issues")]
    if bad:
        lines += ["", "## 问题明细", ""]
        for t, r in bad:
            lines.append(f"**{t['case_id']}**　{r['verdict']} · {r['blame']}")
            for i in r["issues"]:
                q = f"「{i['quote']}」 " if i.get("quote") else ""
                lines.append(f"- {q}{i['detail']}")
            lines.append("")
    return "\n".join(lines) + "\n"


# ── HTTP ──────────────────────────────────────────────────────────────
def iso(ts):
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds") if ts else None


class App:
    def __init__(self, data_dir: Path, llm, keys: dict[str, str], workers: int):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(data_dir / "jobs.db")
        self.runner = Runner(self.store, llm, data_dir, workers)
        self.llm, self.keys, self.workers, self.t0 = llm, keys, workers, time.time()
        self.runner.start()

    def view(self, j: dict) -> dict:
        meta = json.loads(j.get("meta") or "{}")
        return {"id": j["id"], "status": j["status"], "progress": {"done": self.store.done_count(j["id"]), "total": j["total"]},
                "queue_position": self.store.queue_position(j["id"]), "options": json.loads(j["options"]),
                "warnings": json.loads(j["warnings"]), "created": iso(j["created"]), "started": iso(j["started"]),
                "finished": iso(j["finished"]), "error": j["error"],
                # 已完成的任务报判定时记下的版本；判据改过之后，旧任务仍显示它当时用的那一版
                "rubric": meta.get("rubric") or judge.rubric_fingerprint(), "endpoint": meta.get("endpoint") or self.llm.name,
                "summary": meta.get("summary") or judge.summarize(self.store.results(j["id"]))}

    def owner(self, headers) -> str | None:
        auth = headers.get("Authorization") or ""
        tok = auth[7:].strip() if auth.startswith("Bearer ") else ""
        for k, name in self.keys.items():
            if tok and hmac.compare_digest(tok, k):
                return name
        return None

    def handle(self, method, url, headers, body):
        u = urlparse(url)
        q = parse_qs(u.query)
        if method == "GET" and u.path == "/healthz":
            out = {"ok": True, "rubric": judge.rubric_fingerprint(), "endpoint": self.llm.name, "workers": self.workers,
                   "running": self.runner.running, "uptime_s": round(time.time() - self.t0)}
            if q.get("upstream") == ["1"]:
                try:
                    self.llm.complete("只回复 ok", "ok", 0.0)
                    out["upstream"] = "ok"
                except Exception as e:
                    out["upstream"], out["ok"] = f"{type(e).__name__}: {str(e)[:120]}", False
            return 200, out
        who = self.owner(headers)
        if not who:
            return 401, {"error": "缺少或错误的 API key：请求头 Authorization: Bearer <key>"}
        if method == "GET" and u.path == "/v1/rubric":
            return 200, {"fingerprint": judge.rubric_fingerprint(), "rubric": judge.RUBRIC, "fields": judge.ENUMS}
        if u.path == "/v1/jobs":
            if method == "POST":
                try:
                    turns, opts, warnings = parse_body(body, headers.get("Content-Type"), q)
                except BadRequest as e:
                    return 400, {"error": str(e)}
                jid = self.store.create(who, turns, opts, warnings)
                self.runner.poke()
                return 202, self.view(self.store.job(jid))
            if method == "GET":
                return 200, {"jobs": [self.view(j) for j in self.store.jobs(who)]}
        m = JOB_PATH.match(u.path)
        if m:
            j = self.store.job(m.group(1))
            if not j or j["owner"] != who:
                return 404, {"error": "没有这个任务"}
            sub = m.group(2)
            if method == "GET" and not sub:
                return 200, self.view(j)
            if method == "GET" and sub == "results":
                rows = [r for r in self.store.results(j["id"]) if r]
                if q.get("format") == ["jsonl"]:
                    return 200, ("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), "application/x-ndjson")
                return 200, {"id": j["id"], "status": j["status"], "results": rows}
            if method == "GET" and sub == "report":
                if j["status"] != "done":
                    return 409, {"error": f"任务还没完成（{j['status']}）"}
                meta = json.loads(j["meta"] or "{}")
                return 200, (report_md(j, self.store.turns(j["id"]), self.store.results(j["id"]), meta), "text/markdown")
            if method == "POST" and sub == "cancel":
                if j["status"] in ("queued", "running"):
                    self.store.set(j["id"], cancel=1, **({"status": "cancelled", "finished": time.time()} if j["status"] == "queued" else {}))
                return 200, self.view(self.store.job(j["id"]))
        return 404, {"error": "没有这个接口"}


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        def _go(self, method):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._send(413, {"error": "请求体太大"})
            body = self.rfile.read(n) if n else b""
            try:
                code, payload = app.handle(method, self.path, self.headers, body)
            except Exception as e:
                code, payload = 500, {"error": f"{type(e).__name__}: {e}"}
            self._send(code, payload)

        def _send(self, code, payload):
            if isinstance(payload, tuple):
                data, ctype = payload[0].encode("utf-8"), payload[1] + "; charset=utf-8"
            else:
                data, ctype = json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8"), "application/json; charset=utf-8"
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._go("GET")

        def do_POST(self):
            self._go("POST")

        def log_message(self, fmt, *args):
            print(f"{datetime.now():%F %T} {self.address_string()} {fmt % args}", flush=True)
    return H


def parse_keys(spec: str) -> dict[str, str]:
    """LIQ_JUDGE_KEYS="alice:k1,bob:k2" → {k1: alice, k2: bob}"""
    out = {}
    for part in (spec or "").split(","):
        part = part.strip()
        if part:
            name, _, key = part.rpartition(":")
            out[key] = name or key[:6]
    return out


def serve(host: str, port: int, data_dir: Path, llm, keys: dict[str, str], workers: int) -> int:
    if not keys:
        raise SystemExit("没有配置调用方 key（LIQ_JUDGE_KEYS 或 service.keys）")
    app = App(data_dir, llm, keys, workers)
    srv = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"强平 judge 服务 http://{host}:{port}  模型 {llm.name}  并发 {workers}  判据 {judge.rubric_fingerprint()}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
