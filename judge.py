# -*- coding: utf-8 -*-
"""强平回答判定：一轮强平问答 → 结构化结论。单轮可多次采样投票。

输入一轮（字段说明见 强平judge.md「三、输入格式」）：
  query          必须  用户这一轮的问题
  answer         必须  客服最终回答
  tool_calls     建议  [{"name": ..., "input": {...}, "output": "..."}]，强平工具的调用和原始返回；[] 表示确实没调用
  evidence_text  可选  agent 当轮看到的材料原文。没有 tool_calls 时当作强平工具的返回来判
  history        建议  对话历史，字符串，或 [{"role": "user"|"assistant", "content": ...}]
  asked_at       建议  用户提问的时间，如 "2026-10-02 13:40:00"，用来判断工具取到的是不是提问之后的强平
  truth          可选  订单真值：字符串，或 {"facts": [{"item","truth"}], "orders": "..."}
  route          可选  路由分类，如 "交易与合约|强平"
  case_id        可选
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIQ_TOOL = "analyze_deepcoin_liquidation"

ENUMS = {
    "event_match": ["一致", "不一致", "无法判断", "不适用"],
    "grounding": ["忠实", "有出入", "编造", "不适用"],
    "failure_handling": ["如实告知", "编造", "不适用"],
    "answers_question": ["是", "部分", "否"],
    "verdict": ["通过", "部分", "不通过"],
    "blame": ["服务", "Agent", "路由", "无"],
}
# 投票打平时取更严重的那个
SEVERITY = {
    "verdict": ["不通过", "部分", "通过"],
    "grounding": ["编造", "有出入", "忠实", "不适用"],
    "failure_handling": ["编造", "如实告知", "不适用"],
    "event_match": ["不一致", "无法判断", "一致", "不适用"],
    "answers_question": ["否", "部分", "是"],
    "blame": ["Agent", "服务", "路由", "无"],
}
SYNONYM = {
    "event_match": {"匹配": "一致", "对": "一致", "是": "一致", "正确": "一致", "相符": "一致",
                    "不匹配": "不一致", "不对": "不一致", "否": "不一致", "错误": "不一致", "不相符": "不一致",
                    "未知": "无法判断", "无": "不适用"},
    "grounding": {"准确": "忠实", "一致": "忠实", "部分忠实": "有出入", "不一致": "有出入"},
    "answers_question": {"部分回答": "部分", "否定": "否"},
}

RUBRIC = """你是加密货币交易所客服质检员，只评估「强平」类问答。
客服链路：客服 Agent 调用强平分析工具（工具再调强平服务）拿到结果后回答用户。你要判断这一轮回答的质量和问题出在哪一层。

你会拿到：用户当前问题、对话历史、该用户的订单真值（可能没有）、工具状态与工具原始返回、客服最终回答。
订单真值来自交易系统的强平订单，是事实标准；「提问后」标记的订单发生在用户提问之后，不是用户在问的那次。
给了提问时间时，工具返回的强平事件如果发生在提问时间之后，一定不是用户在问的那次，event_match 填「不一致」，并在 issues 里写明「取到的是提问之后的强平」。
工具状态为「未提供」时，说明调用方没有给出工具调用信息，不代表没调用：event_match 填「无法判断」，failure_handling 填「不适用」，blame 不能填「路由」，只依据对话历史和订单真值判断回答本身。
没有订单真值时，以对话历史和工具返回为依据，不要凭常识补全具体数字。

逐项判断：
1. event_match：工具返回里描述的强平事件（时间、币种、方向、笔数），是不是用户在问的那一次。
   用户在问哪一次，依据对话历史里的截图 OCR、时间、币种、价格和订单真值确定。
   工具没给出具体事件、只给通用解释时填「无法判断」；工具失败或未调用填「不适用」。
2. grounding：最终回答里的事实（价格、数量、余额、亏损、时间、币种、"同时还有某仓位"、机制性断言如"维持保证金进入保险基金""没有触发强平"）
   - 都能在工具返回、对话历史或订单真值里找到，且不与订单真值矛盾 →「忠实」
   - 有个别数字或事实与工具返回/订单真值不一致，或把工具的错误事件当事实写进回答 →「有出入」
   - 出现三处来源都没有、且是在下结论的说法（如凭空断言"没有发生强平"、编造扣费构成、编造平台政策）→「编造」
   通用的强平机制解释（保证金不足触发强平、全仓共享保证金等）本身不算编造。
3. failure_handling：工具失败（超时、报错文本、[查询失败]）时，回答是否只说暂时无法查询。
   如实告知 → 「如实告知」；继续给出原因或结论 → 「编造」；工具没失败填「不适用」。
4. answers_question：是否回应了用户这句话的具体疑问（例如"为什么亏 1300 不是 700""为什么还差 10 个点就被平"）。
5. verdict：
   - 通过：事件对或无需判断，grounding 忠实，回应了具体疑问（工具失败时如实告知也算通过）
   - 不通过：编造，或把错误事件/错误数字当事实告诉用户，或工具失败时编造原因
   - 其余为部分
6. blame：问题主要出在哪一层。
   服务：工具返回本身就错/不全（取错事件、只有通用解释、无数字、超时、报错）而回答只是转述
   Agent：工具返回是对的或已失败，回答却改错数字、编造、或违背工具结论
   路由：没有调用强平工具
   无：通过
7. issues：每个问题一条，quote 必须是最终回答里的原文片段（没有就留空），detail 说明错在哪、真值是什么。"""

OUTPUT = """只输出一个 JSON 对象，不要输出其他文字：
{"event_match":"一致|不一致|无法判断|不适用","grounding":"忠实|有出入|编造|不适用","failure_handling":"如实告知|编造|不适用","answers_question":"是|部分|否","verdict":"通过|部分|不通过","blame":"服务|Agent|路由|无","issues":[{"quote":"","detail":""}],"why":"一句话结论"}
每个字段只能从 | 分隔的选项里选一个，原样照写，不要用同义词。"""

SYSTEM = RUBRIC + "\n\n" + OUTPUT


def rubric_fingerprint() -> str:
    return hashlib.sha256(SYSTEM.encode()).hexdigest()[:12]


# ── 模型端点 ──────────────────────────────────────────────────────────
def load_env(path: Path = HERE / "config.env") -> None:
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def vpn_iface(prefix: str = "10.8.8.") -> str:
    """找 VPN 隧道接口：地址以 10.8.8. 开头的那个 utun。VPN 重连后地址会变、接口名一般不变，所以每次现找。"""
    import subprocess
    out = subprocess.run(["ifconfig"], capture_output=True, text=True).stdout
    cur = ""
    for line in out.splitlines():
        if line and not line[0].isspace():
            cur = line.split(":", 1)[0]
        elif cur.startswith("utun") and line.strip().startswith("inet " + prefix):
            return cur
    return ""


class GLM:
    """OpenAI 兼容端点（glm-5.3）。不要在请求体里加 enable_thinking=false：这个部署会把推理漏进正文。"""

    def __init__(self) -> None:
        self.base = os.environ.get("JUDGE_BASE_URL", "").rstrip("/")
        self.key = os.environ.get("JUDGE_API_KEY", "")
        self.model = os.environ.get("JUDGE_MODEL", "glm-5.3")
        self.timeout = int(os.environ.get("JUDGE_TIMEOUT", "240"))
        self.iface = os.environ.get("JUDGE_IFACE", "")          # 本机走 VPN 隧道时设成 auto 或 utun10；服务器上留空
        if self.iface == "auto":
            self.iface = vpn_iface()
            if not self.iface:
                raise SystemExit("JUDGE_IFACE=auto，但没找到 VPN 隧道接口（地址 10.8.8.x 的 utun）。先连 VPN")
        self.extra = json.loads(os.environ.get("JUDGE_EXTRA_BODY") or "{}")
        if not self.base:
            raise SystemExit("没有配置 JUDGE_BASE_URL（liq_judge/config.env 或环境变量）")
        self.name = self.model
        self.fingerprint = f"{self.model}|{self.base}|{json.dumps(self.extra, sort_keys=True)}"

    def complete(self, system: str, user: str, temperature: float) -> str:
        body = json.dumps({"model": self.model, "temperature": temperature,
                           "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                           **self.extra}).encode()
        if self.iface:
            cmd = ["curl", "-sS", "--noproxy", "*", "--interface", self.iface, "--max-time", str(self.timeout),
                   "-H", f"Authorization: Bearer {self.key}", "-H", "Content-Type: application/json",
                   "-d", "@-", f"{self.base}/chat/completions"]
            p = subprocess.run(cmd, input=body, capture_output=True)
            if p.returncode != 0:
                raise RuntimeError(f"调用失败：curl rc={p.returncode} {p.stderr.decode('utf-8', 'replace')[:160]}")
            raw = p.stdout
        else:
            req = urllib.request.Request(f"{self.base}/chat/completions", data=body, headers={
                "Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with opener.open(req, timeout=self.timeout) as r:
                    raw = r.read()
            except Exception as e:
                raise RuntimeError(f"调用失败：{type(e).__name__}: {str(e)[:160]}")
        d = json.loads(raw.decode("utf-8", "replace"))
        return str(((d.get("choices") or [{}])[0].get("message") or {}).get("content") or "")


class Mock:
    """不发请求，自检用：工具失败判如实告知，其余判通过。"""
    name = fingerprint = "mock"

    def complete(self, system: str, user: str, temperature: float) -> str:
        failed = "【工具状态】失败" in user
        missing = "【工具状态】未调用" in user
        unknown = "【工具状态】未提供" in user
        return json.dumps({"event_match": "无法判断" if unknown else ("不适用" if (failed or missing) else "一致"),
                           "grounding": "忠实", "failure_handling": "如实告知" if failed else "不适用",
                           "answers_question": "是", "verdict": "部分" if missing else "通过",
                           "blame": "路由" if missing else "无", "issues": [], "why": "mock"}, ensure_ascii=False)


# ── 回复解析 ──────────────────────────────────────────────────────────
def _objects(txt: str):
    depth, start, in_str, esc = 0, -1, False, False
    for i, ch in enumerate(txt):
        if in_str:
            esc = (ch == "\\") and not esc
            if ch == '"' and not esc:
                in_str = False
            continue
        if ch == '"':
            in_str, esc = True, False
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                yield txt[start:i + 1]


def parse_json(raw: str) -> dict:
    """取回复里最后一个可解析的 JSON 对象。先切掉 </think> 之前的推理。"""
    txt = str(raw or "")
    if "</think>" in txt:
        txt = txt.rsplit("</think>", 1)[1]
    txt = re.sub(r"<think>.*", "", txt, flags=re.S)
    txt = re.sub(r"```(?:json)?", "", txt).strip()
    try:
        return json.loads(txt)
    except ValueError:
        pass
    for cand in reversed(list(_objects(txt))):
        try:
            return json.loads(cand)
        except ValueError:
            continue
    raise ValueError("回复里没有可解析的 JSON：" + txt[:120])


def validate(o) -> dict:
    if not isinstance(o, dict):
        raise ValueError("不是对象")
    for k, m in SYNONYM.items():
        v = str(o.get(k, "")).strip()
        o[k] = m.get(v, v)
    for k, allowed in ENUMS.items():
        if o.get(k) not in allowed:
            raise ValueError(f"{k}={o.get(k)!r} 不在 {allowed}")
    o["issues"] = [{"quote": str(i.get("quote") or ""), "detail": str(i.get("detail") or "")}
                   for i in (o.get("issues") or []) if isinstance(i, dict)]
    o["why"] = str(o.get("why") or "")
    return o


# ── 组装输入 ──────────────────────────────────────────────────────────
def liq_calls(turn: dict, prefix: str = LIQ_TOOL) -> list[dict]:
    return [c for c in (turn.get("tool_calls") or []) if str(c.get("name", "")).startswith(prefix)]


def tool_status(turn: dict, prefix: str = LIQ_TOOL) -> tuple[str, dict | None]:
    """正常 / 失败：取第一次强平工具调用判；未调用：给了 tool_calls 但里面没有强平工具；
    未提供：既没有 tool_calls 字段也没有 evidence_text。没有 tool_calls 但有 evidence_text 时，把材料当工具返回。"""
    if turn.get("tool_calls") is None:
        ev = str(turn.get("evidence_text") or "")
        if not ev.strip():
            return "未提供", None
        c = {"name": "(evidence_text)", "input": None, "output": ev}
    else:
        calls = liq_calls(turn, prefix)
        if not calls:
            return "未调用", None
        c = calls[0]
    out = str(c.get("output") or "")
    failed = (not out.strip() or "tool invoke error" in out or "分析过程出现错误" in out
              or "[查询失败]" in out or bool(c.get("error")))
    return ("失败" if failed else "正常"), c


def fmt_history(h, limit: int = 6000) -> str:
    if not h:
        return "（无）"
    if isinstance(h, str):
        s = h
    else:
        s = "\n".join(f"{'用户' if m.get('role') == 'user' else '客服'}{(' ' + m['time']) if m.get('time') else ''}："
                      f"{m.get('content', '')}" for m in h if isinstance(m, dict))
    return s if len(s) <= limit else "……（更早的历史已截断）\n" + s[-limit:]


def fmt_truth(t) -> str:
    if not t:
        return "（未提供）"
    if isinstance(t, str):
        return t
    parts = []
    for f in t.get("facts") or []:
        parts.append(f"- {f.get('item', '')}：{f.get('truth', '')}")
    if t.get("orders"):
        parts.append("该用户强平订单：\n" + str(t["orders"]))
    return "\n".join(parts) or "（未提供）"


def build_prompt(turn: dict, prefix: str = LIQ_TOOL) -> tuple[str, str]:
    status, call = tool_status(turn, prefix)
    user = f"""【用户当前问题】
{turn['query']}

【提问时间】{turn.get('asked_at') or '（未提供）'}

【对话历史】
{fmt_history(turn.get('history'))}

【订单真值】
{fmt_truth(turn.get('truth'))}

【工具状态】{status}
【工具入参】{json.dumps(call.get('input'), ensure_ascii=False)[:600] if call else '（未调用）'}
【工具原始返回】
{str(call.get('output') or '')[:4000] if call else ('（调用方没有提供工具信息）' if status == '未提供' else '（未调用）')}

【客服最终回答】
{turn.get('answer') or '（空）'}"""
    return user, status


# ── 判定一轮 ──────────────────────────────────────────────────────────
class Cache:
    """按 (模型指纹, 判据, 样本序号, prompt) 缓存原始回复，只缓存校验通过的。"""

    def __init__(self, path: Path | None):
        self.path, self.mem, self.lock = path, {}, threading.Lock()
        if path and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    x = json.loads(line)
                    self.mem[x["k"]] = x["raw"]
                except (ValueError, KeyError):
                    pass

    def call(self, llm, user: str, sample: int, temperature: float, retries: int = 2) -> tuple[dict, str]:
        k = hashlib.sha256(f"{llm.fingerprint}\x00{SYSTEM}\x00{sample}\x00{temperature}\x00{user}".encode()).hexdigest()
        if k in self.mem:
            try:
                return validate(parse_json(self.mem[k])), ""
            except ValueError:
                pass
        err = ""
        for attempt in range(retries + 1):
            try:
                raw = llm.complete(SYSTEM, user, temperature)
                o = validate(parse_json(raw))
                if self.path:
                    with self.lock:
                        self.mem[k] = raw
                        with self.path.open("a", encoding="utf-8") as f:
                            f.write(json.dumps({"k": k, "raw": raw}, ensure_ascii=False) + "\n")
                return o, ""
            except Exception as e:
                err = f"{type(e).__name__}: {str(e)[:160]}"
                time.sleep(1.5 * (attempt + 1))
        return {}, err


def _vote(values: list[str], field: str) -> str:
    c = Counter(values)
    top = max(c.values())
    tied = [v for v in c if c[v] == top]
    order = SEVERITY[field]
    return sorted(tied, key=lambda v: order.index(v) if v in order else 99)[0]


def judge_turn(turn: dict, llm, cache: Cache, samples: int = 3, tool_prefix: str = LIQ_TOOL) -> dict:
    """一轮 → 结论。多次采样时每个字段各自投票，打平取更严重的；issues 取与最终结论一致的那次采样。
    责任层「路由」只在确实没调用强平工具时成立，由代码兜底。"""
    t0 = time.time()
    user, status = build_prompt(turn, tool_prefix)
    outs, errs = [], []
    for i in range(samples):
        temp = 0.0 if i == 0 else 0.6
        o, err = cache.call(llm, user, i, temp)
        (outs.append(o) if o else errs.append(err))
    res = {"case_id": turn.get("case_id"), "tool_status": status, "samples": samples,
           "judged": len(outs), "judge_errors": errs, "complete": len(outs) > samples // 2,
           "undecidable": ["event_match", "failure_handling"] if status == "未提供" else []}
    if not outs:
        res.update({k: None for k in ENUMS})
        res.update({"issues": [], "why": "", "votes": {}})
    else:
        final = {k: _vote([o[k] for o in outs], k) for k in ENUMS}
        if status == "未调用" and final["verdict"] != "通过":
            final["blame"] = "路由"
        elif status != "未调用" and final["blame"] == "路由":
            final["blame"] = "Agent" if final["grounding"] == "编造" else "服务"
        if status == "未提供":
            final["event_match"], final["failure_handling"] = "无法判断", "不适用"
        pick = next((o for o in outs if all(o[k] == final[k] for k in ("verdict", "blame"))), outs[0])
        res.update(final)
        res.update({"issues": pick["issues"], "why": pick["why"],
                    "votes": {k: dict(Counter(o[k] for o in outs)) for k in ("verdict", "blame", "grounding")}})
    res["elapsed_ms"] = round((time.time() - t0) * 1000)
    return res


def summarize(rows: list[dict | None]) -> dict:
    done = [r for r in rows if r]
    return {k: dict(Counter(r.get(k) for r in done if r.get(k))) for k in ("verdict", "blame", "grounding",
                                                                           "event_match", "failure_handling",
                                                                           "answers_question")} | {
        "incomplete": sum(1 for r in done if not r.get("complete"))}
