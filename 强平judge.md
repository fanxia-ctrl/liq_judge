# 调用强平 judge 服务

强平 judge 是一个自动评测服务：把客服「强平」类问答的每一轮交给它，它判断工具取到的强平事件对不对、
回答里的事实有没有出处、工具失败时有没有乱说、有没有回应用户的具体疑问，给出总判定和问题出在哪一层
（强平服务、客服 Agent、路由），并附回答原文引用和理由。

trace 来自哪里都可以：线上日志、Langfuse、Dify 的事件流、其他系统的对话记录。只要按「二」取出几项内容、整理成一行一轮的文件，就能提交。
调用方式与文本 judge 服务相同：提交一批轮次，拿到任务 id，轮询，完成后取结果和报告。

> judge 的结论用于辅助人工复核和版本回归对比，不替代人工审核。

---

## 一、整体流程

```
① 你的 trace（线上日志、Langfuse、Dify 事件流……）
                                    │
② 按「二」取出字段，整理成 turns.jsonl（一行一轮）
                                    │
③ ──HTTP POST──▶ 强平 judge 服务 ──▶ 返回任务 id
                                    │
④ 轮询任务状态，完成后取结果（逐轮结论、汇总报告）
```

---

## 二、从 trace 里取哪些字段

judge 判定时依据的是「客服这一轮实际看到了什么、说了什么」。不管 trace 来自哪里，取下面几项：

| 要取的内容 | 对应字段 | 说明 |
|---|---|---|
| 用户这一轮的原话 | `query` | 必须 |
| 客服最终发给用户的回答 | `answer` | 必须，取最终文本，不是中间步骤 |
| 强平工具的调用：工具名、入参、**原始返回** | `tool_calls` | 返回要取 agent 收到的原文，不要摘要或截断。这是判断「服务错了还是 Agent 错了」的依据 |
| 或者：agent 这一轮看到的全部材料 | `evidence_text` | trace 里工具返回和其他材料混在一起、拆不出单次调用时用这个 |
| 这一轮之前的对话 | `history` | 包括用户发的截图的 OCR 文本。用来确定用户在问哪一次强平 |
| 用户提问的时间 | `asked_at` | trace 里的消息时间。用来识别工具取到的是提问之后才发生的强平 |
| 该用户的强平订单 | `truth` | 交易系统里的强平记录。没有也能判，但取错事件、算错数字基本判不出来 |

几点注意：

- **工具调用一次都没有时，传 `"tool_calls": []`**，judge 会判为「没有调用强平工具」，责任层记为路由。
  **trace 里看不到工具信息时，不要传 `tool_calls`**，judge 会把「事件是否对得上」「工具失败时的处理」两项记为判不了，不会算到路由头上。
- **工具名**：默认识别名称以 `analyze_deepcoin_liquidation` 开头的调用（含 `_v2` 等版本）。你的系统里工具名不同时，提交时用 `tool_name` 选项指定。
- **工具返回里的报错**：返回为空、含 `tool invoke error`、`分析过程出现错误` 或以 `[查询失败]` 开头时，判为工具失败，看客服有没有如实告知。

### 以 Dify 文本 copilot 的事件流为例

Dify `/v1/chat-messages` 以 streaming 模式返回的事件里，各字段这样取：

| 字段 | 事件 | 取法 |
|---|---|---|
| `answer` | `message` | 各事件的 `answer` 拼接 |
| `tool_calls` | `agent_log`，`label` 以 `CALL ` 开头且 `status` 为 `success` / `error` | `data.output.tool_call_name`、`tool_call_input`、`tool_response`。`tool_response` 里的 `variable_value` 才是工具返回原文 |
| `history` | 请求时传入的 `inputs.conversation_history` | 原样带上 |
| `asked_at` | — | 发请求的时间。线上日志里取这条用户消息的发送时间 |
| `route` | `node_finished`，`title` 为 `ROUTER` | `outputs.text`，如 `{"topic":"交易与合约|强平"}` |

其他来源（Langfuse、网关日志等）按同样的含义去找：用户问题、最终回答、工具调用的名称入参和原始返回、之前的对话、提问时间。

---

## 三、输入格式

### 每一轮需要哪些字段

| 字段 | 是否必须 | 缺失时的影响 |
|---|---|---|
| `query` | **必须** | 无法提交 |
| `answer` | **必须** | 无法提交 |
| `tool_calls` 或 `evidence_text` | 建议提供 | 两项判不了（记在 `undecidable`）；回答里的数字在材料里找不到出处，容易被判为编造 |
| `history` | 建议提供 | 无法确定用户在问哪一次强平，「事件是否对得上」判得偏弱 |
| `asked_at` | 建议提供 | 工具取到提问之后才发生的强平时，只能靠订单真值识别 |
| `truth` | 建议提供 | 只能对照工具返回和对话历史，工具取错事件、算错数字可能判不出来 |
| `route` | 可选 | 只做记录，不影响判定 |
| `case_id` | 可选 | 不提供时按顺序编号为 `t0001`、`t0002`…；提供时同一批内不能重复 |

**`tool_calls` 的写法：**数组，每个元素是一次工具调用。judge 只看名称匹配的强平工具调用，取第一次：

```json
{"name": "analyze_deepcoin_liquidation_v2",
 "input": {"uid": "<uid>", "content": "为什么我的单会强平？", "context": "……"},
 "output": "工具返回的原文"}
```

**`evidence_text` 的写法：**字符串，agent 当轮看到的材料原文。只在没有 `tool_calls` 时使用。

**`history` 的写法：**字符串，或 `[{"role": "user" | "assistant", "content": "……"}]`。

**`asked_at` 的写法：**字符串，如 `"2026-10-02 13:40:00"`，北京时间。

**`truth` 的写法：**字符串，或 `{"facts": [{"item": "强平触发价", "truth": "2754.15"}], "orders": "订单表文本"}`。

提交时，服务会在返回的 `warnings` 中列出缺失的字段及其影响，请留意。

### 两种提交格式

请求头 `Content-Type` 告诉服务请求体是什么格式，两种任选其一，内容完全等价。

**`application/x-ndjson`**：请求体为多行文本，每行一个 JSON 对象，代表一轮。即 `turns.jsonl` 文件本身，可以直接上传。
任务选项放在 URL 参数中，如 `?name=liq-regression&samples=3`。URL 参数中的任务名建议只用英文、数字和连字符；需要中文名时，改用 `application/json` 格式放在 `options` 里。

```text
{"case_id": "liq-001", "query": "50个以太坊13个点就强平了……", "answer": "……", "history": [...], "tool_calls": [...], "asked_at": "……"}
{"case_id": "liq-002", "query": "为什么没有强平", "answer": "……", "evidence_text": "……", "asked_at": "……"}
```

**`application/json`**：请求体为一个 JSON 对象，轮次放在 `turns` 数组中，任务选项放在 `options` 中。

```json
{
  "turns": [
    {"case_id": "liq-001", "query": "……", "answer": "……", "tool_calls": [], "history": [], "asked_at": "……"}
  ],
  "options": {"name": "十月强平回归", "samples": 3}
}
```

一次最多提交 2000 轮。输入有误时整批退回（HTTP 400），错误信息会指出第几轮的哪个字段。

### 任务选项

| 选项 | 默认值 | 说明 |
|---|---|---|
| `name` | 空 | 任务名称，会出现在报告标题中 |
| `samples` | 3 | 每轮判定次数（1～5），每个字段各自投票，打平取更严重的结论。设为 1 速度约快三倍，但结果稳定性下降，不建议用于正式对比 |
| `tool_name` | `analyze_deepcoin_liquidation` | 强平工具的名称前缀。你的系统里工具名不同时指定 |

---

## 四、调用服务

### 地址与访问凭证

| | |
|---|---|
| 服务地址 | `http://<dev4 地址>:8788`（部署到 dev4 后填写） |
| 访问凭证 | 请向服务维护人申请 API key |

除健康检查外，所有请求都需要携带请求头 `Authorization: Bearer <你的 key>`。每个 key 只能查看自己提交的任务。

以下示例先设置两个环境变量：

```bash
export LIQ_JUDGE_URL=http://<dev4 地址>:8788
export LIQ_JUDGE_KEY=<你的 key>
```

> 如果你的电脑设置了 HTTP 代理（如 Clash 等工具），请在每条 curl 命令中加上 `--noproxy '*'`。

### 第 0 步：确认服务可用

```bash
curl -s $LIQ_JUDGE_URL/healthz
```

返回 `"ok": true` 即表示服务正常。加 `?upstream=1` 会真调一次模型端点，确认 judge 后端也可用。

### 第 1 步：提交

```bash
curl -s -X POST "$LIQ_JUDGE_URL/v1/jobs?name=liq-regression" \
     -H "Authorization: Bearer $LIQ_JUDGE_KEY" \
     -H "Content-Type: application/x-ndjson" \
     --data-binary @turns.jsonl
```

服务立即返回 HTTP 202，不会等待判定完成。返回内容中的 `id` 是任务编号，后续查询都用它。

### 第 2 步：查询进度

```bash
curl -s $LIQ_JUDGE_URL/v1/jobs/<任务id> -H "Authorization: Bearer $LIQ_JUDGE_KEY"
```

`status` 的取值：

| 值 | 含义 |
|---|---|
| `queued` | 排队中，`queue_position` 表示前面还有几个任务 |
| `running` | 判定中，`progress` 显示已完成轮数 |
| `done` | 已完成 |
| `failed` | 失败，原因见 `error` |
| `cancelled` | 已取消，已完成的轮次结果仍可获取 |

建议每 10～30 秒查询一次。断开连接不影响任务，服务重启后未完成的任务会接着判，已判完的轮次不重判。

### 第 3 步：获取结果

```bash
# 逐轮结论（JSON）
curl -s $LIQ_JUDGE_URL/v1/jobs/<任务id>/results -H "Authorization: Bearer $LIQ_JUDGE_KEY"

# 逐轮结论（一行一轮，便于保存为文件）
curl -s "$LIQ_JUDGE_URL/v1/jobs/<任务id>/results?format=jsonl" -H "Authorization: Bearer $LIQ_JUDGE_KEY" > results.jsonl

# 汇总报告（Markdown）
curl -s $LIQ_JUDGE_URL/v1/jobs/<任务id>/report -H "Authorization: Bearer $LIQ_JUDGE_KEY" > report.md
```

### 其他操作

```bash
# 查看自己提交过的任务
curl -s $LIQ_JUDGE_URL/v1/jobs -H "Authorization: Bearer $LIQ_JUDGE_KEY"

# 取消任务
curl -s -X POST $LIQ_JUDGE_URL/v1/jobs/<任务id>/cancel -H "Authorization: Bearer $LIQ_JUDGE_KEY"

# 判定标准全文和判据指纹
curl -s $LIQ_JUDGE_URL/v1/rubric -H "Authorization: Bearer $LIQ_JUDGE_KEY"
```

### 在程序中调用（Python 示例）

```python
import time
import requests

URL = "http://<dev4 地址>:8788"
HEADERS = {"Authorization": "Bearer <你的 key>"}

with open("turns.jsonl", "rb") as f:
    job = requests.post(f"{URL}/v1/jobs?name=liq-regression", data=f,
                        headers={**HEADERS, "Content-Type": "application/x-ndjson"}).json()
print("任务", job["id"], "提示：", job["warnings"])

while job["status"] not in ("done", "failed", "cancelled"):
    time.sleep(15)
    job = requests.get(f"{URL}/v1/jobs/{job['id']}", headers=HEADERS).json()
    print(job["status"], job["progress"])

results = requests.get(f"{URL}/v1/jobs/{job['id']}/results", headers=HEADERS).json()["results"]
for r in results:
    print(r["case_id"], r["verdict"], r["blame"], r["why"])
```

---

## 五、结果说明

### 每一轮的结论

| 字段 | 取值 | 说明 |
|---|---|---|
| `tool_status` | 正常 / 失败 / 未调用 / 未提供 | 强平工具这一轮的状态，由规则判定，不经过模型。未提供表示调用方没有给工具信息 |
| `event_match` | 一致 / 不一致 / 无法判断 / 不适用 | 工具返回的强平事件（时间、币种、方向、笔数）是不是用户在问的那一次。只有通用解释时为无法判断；工具失败或未调用为不适用 |
| `grounding` | 忠实 / 有出入 / 编造 / 不适用 | 回答里的价格、数量、余额、亏损、时间、币种和结论性说法，能否在工具返回、对话历史或订单真值里找到且不矛盾 |
| `failure_handling` | 如实告知 / 编造 / 不适用 | 工具失败时，回答是否只说暂时无法查询 |
| `answers_question` | 是 / 部分 / 否 | 是否回应了用户这句话的具体疑问，如「为什么亏 1300 不是 700」 |
| `verdict` | 通过 / 部分 / 不通过 | 总判定，见下表 |
| `blame` | 服务 / Agent / 路由 / 无 | 问题主要出在哪一层，见下表 |
| `issues` | 列表 | 每条包含回答原文 `quote` 和说明 `detail`（错在哪、真值是什么） |
| `why` | 文本 | 一句话结论 |
| `votes` | 对象 | 多次判定时 `verdict`、`blame`、`grounding` 各自的票数 |
| `undecidable` | 列表 | 因缺少材料判不了的项。没有工具信息时为 `event_match`、`failure_handling` |
| `complete` | true / false | 为 `false` 时表示多数判定调用失败，结论仅供参考，原因见 `judge_errors` |

`verdict` 的取值：

| 结论 | 判定规则 |
|---|---|
| 通过 | 事件对得上或无需判断，回答有出处，回应了具体疑问。工具失败时如实告知也算通过 |
| 不通过 | 回答编造，或把错误的事件、数字当事实告诉用户，或工具失败时编造原因 |
| 部分 | 其余情况，例如事实没错但没回答到用户问的点 |

`blame` 的取值：

| 责任层 | 判定规则 |
|---|---|
| 服务 | 工具返回本身错误或不全（取错事件、只有通用解释、没有数字、超时、报错），回答只是转述 |
| Agent | 工具返回是对的或已失败，回答却改错数字、编造，或违背工具的结论 |
| 路由 | 强平问题没有调用强平工具（`tool_calls` 为空数组）。由代码判定，没给工具信息时不会记为路由 |
| 无 | 通过 |

### 版本标识

任务信息中有两个版本标识，建议与结果一起保存：

| 字段 | 含义 |
|---|---|
| `rubric` | 判据指纹。判定标准调整后会变化 |
| `endpoint` | judge 所用模型 |

对比两批结果时，请先确认这两个标识一致。任意一个不同，结果之间的差异就可能来自 judge 本身，而不是被测 copilot。

---

## 六、完整示例

以下是一次真实调用：提交 11 轮，采用默认配置（每轮判定 3 次）。示例中的编号和 uid 已替换。

**提交的一轮**（节选）：

```json
{
  "case_id": "liq-demo-01",
  "query": "我明明设置了止损，为什么给我强平",
  "answer": "……您这笔ETHUSDT多单，开仓均价2737.04，系统算出的强平价是2710.24。但您的止损价设在了406.515，这个价格远低于强平价。价格下跌时，先触及2710.24触发强平，根本没走到406.515，所以止损单没机会执行……",
  "history": [
    {"role": "user", "content": "User uploaded an image. OCR result: ……记录1（强平多）：委托量 200 张，强平价 2,710.24，盈亏 -753.095 …… 记录2（平多-止盈止损）：委托价 2,711，已撤单 …… 记录4（平多-止盈止损）：委托价 2,735，盈亏 406.515 ……"},
    {"role": "user", "content": "我明明设置了止损，为什么给我强平"}
  ],
  "tool_calls": [{"name": "analyze_deepcoin_liquidation_v2",
                  "input": {"uid": "<uid>", "content": "我明明设置了止损，为什么给我强平", "context": "……"},
                  "output": "……但您的止损价设在了406.515，这个价格远低于强平价……"}],
  "truth": {"facts": [{"item": "合约 / 方向 / 模式", "truth": "ETHUSDT 多 全仓 125x"},
                      {"item": "强平触发价", "truth": "2710.24（成交 2699.39）"},
                      {"item": "亏损", "truth": "−753.10"}]}
}
```

**提交：**

```bash
curl -s -X POST "$LIQ_JUDGE_URL/v1/jobs?name=liq-demo" \
     -H "Authorization: Bearer $LIQ_JUDGE_KEY" \
     -H "Content-Type: application/x-ndjson" \
     --data-binary @turns.jsonl
```

那一轮 copilot 把问题分到了「带单」，没有调用强平工具，提交时 `tool_calls` 为空数组，judge 记为路由的问题。

**约 3.5 分钟后任务完成，查询任务状态：**

```json
{
  "id": "20261012-193847-c89296",
  "status": "done",
  "progress": {"done": 11, "total": 11},
  "rubric": "298b458d11e1",
  "endpoint": "glm-5.3",
  "summary": {
    "verdict": {"不通过": 7, "通过": 2, "部分": 2},
    "blame": {"服务": 7, "无": 2, "Agent": 1, "路由": 1},
    "grounding": {"有出入": 8, "忠实": 3},
    "event_match": {"一致": 5, "不一致": 5, "不适用": 1},
    "failure_handling": {"不适用": 11},
    "answers_question": {"部分": 9, "是": 2},
    "incomplete": 0
  }
}
```

**获取结果（节选两轮）：**

```json
[
  {
    "case_id": "liq-demo-01",
    "tool_status": "正常",
    "verdict": "不通过",
    "blame": "服务",
    "event_match": "一致",
    "grounding": "有出入",
    "failure_handling": "不适用",
    "answers_question": "部分",
    "votes": {"verdict": {"不通过": 3}, "blame": {"服务": 3}, "grounding": {"有出入": 3}},
    "why": "工具把盈亏 406.515 误判为止损价，客服未对照历史截图核实便照抄，导致对用户「设了止损为何还被强平」的解释在关键事实上完全错误。",
    "issues": [
      {"quote": "但您的止损价设在了406.515，这个价格远低于强平价",
       "detail": "406.515 是用户此前一笔止盈止损单的盈亏金额（USDT），不是止损价。对话历史 OCR 显示用户的止损委托价为 2,711，高于强平价 2,710.24，且该止损单在强平前 2 秒状态为已撤单。工具把盈亏数字误当止损价，回答原样照抄了这一错误事实。"}
    ],
    "complete": true
  },
  {
    "case_id": "liq-demo-02",
    "tool_status": "正常",
    "verdict": "通过",
    "blame": "无",
    "event_match": "一致",
    "grounding": "忠实",
    "answers_question": "是",
    "votes": {"verdict": {"通过": 3}, "blame": {"无": 3}, "grounding": {"忠实": 3}},
    "why": "工具返回的强平事件与订单真值一致（ETH 空 2 笔、触发价 2723.03），客服忠实转述并解释了做空时价格先触及 2723.03 强平价、止损 2734 未及触发。",
    "issues": [],
    "complete": true
  }
]
```

**解读：**第一轮中，强平服务把截图里一笔订单的盈亏 406.515 当成了止损价，客服原样转述，用户拿到的解释在关键事实上是错的，所以结论是「不通过」。
回答本身没有改动工具的说法，错误来自工具返回，所以责任层是「服务」。第二轮工具给出了正确的触发价和止损价对比，回答直接回应了「为什么提前 10 个点被平」，结论是「通过」。

同一批里还有一轮三次判定各投了一票（通过、部分、不通过），按规则打平取更严重的，记为「不通过」。这种轮次的结论处在临界，建议人工看一眼。

---

## 七、耗时与限制

- **耗时**：默认配置下每轮判定 3 次，单轮约 15 秒到 2.5 分钟，中位约 1 分钟。多轮并行处理（默认并发 4），11 轮约 3.5 分钟，整体约每小时 150～200 轮。
- **排队**：任务按提交顺序依次处理，前面有较大的任务时需要等待。
- **稳定性**：judge 后端模型偶尔会出现短时不可用。单次判定失败会重试；一轮多数判定都失败时 `complete` 为 `false`，重新提交即可，已判过的内容有缓存，不会重复调用。
- **服务重启**：未完成的任务在重启后接着判，已判完的轮次不重判。

---

## 八、常见问题

**返回 401**
未携带 key，或 key 不正确。检查请求头是否为 `Authorization: Bearer <你的 key>`。

**连接超时或被拒绝**
确认服务地址和端口正确。如果本机设置了 HTTP 代理，在 curl 命令中加 `--noproxy '*'`。

**大量轮次 `blame` 为「路由」**
通常是工具名对不上：你的系统里强平工具不叫 `analyze_deepcoin_liquidation…`，judge 在 `tool_calls` 里找不到它，就当成没调用。提交时用 `tool_name` 指定工具名。

**大量轮次 `undecidable` 不为空，回答被判「编造」**
提交时既没有 `tool_calls` 也没有 `evidence_text`。judge 看不到客服拿到的材料，回答里的数字找不到出处，只能按编造处理。请从 trace 里补上工具返回原文。

**取错事件却判成通过**
没有提供 `truth` 时，judge 只能对照工具返回和对话历史，工具取错事件但说得自洽时可能判不出来。正式对比请带上订单真值。

**`complete` 为 `false`**
多数判定调用失败，通常是 judge 后端模型短时不可用。原因见 `judge_errors`，重新提交即可，已判过的内容有缓存。

**同样的数据两次提交，结果略有不同**
judge 基于大模型判定，存在一定随机性，同一类说法在不同轮次可能判得不同。默认每轮判定 3 次投票，已降低波动，
处于临界的结论仍可能变化。版本对比时请保持相同的 `samples`，关注整体统计而非单轮差异。判定结果尚未与人工标注对齐。

如有其他问题，请联系服务维护人。

---

## 九、部署

服务代码在 `voice_agent_v3` 仓库的 `liq_judge/` 目录，标准库实现，只需要 Python 3.9+。

```bash
git clone git@github.com:fanxia-ctrl/voice_agent_v3.git && cd voice_agent_v3/liq_judge
cp config.env.example config.env      # 填模型端点的 key；服务器上 JUDGE_IFACE 留空
bash serve.sh check                   # 第一次先跑：自检 + 真调一次模型端点
bash serve.sh                         # 后台启动，默认监听 0.0.0.0:8788
```

| 命令 | 作用 |
|---|---|
| `bash serve.sh` | 启动。已经在跑就什么都不做，可以反复执行 |
| `bash serve.sh stop` / `restart` | 停止 / 重启（改了 key、拉了新代码之后） |
| `bash serve.sh status` | 在不在跑 + 健康检查 |
| `bash serve.sh logs` | 跟日志（`data/serve.log`，超过 50MB 自动轮转） |
| `bash serve.sh attach` | 进 tmux 看实时输出，`Ctrl-b` 再按 `d` 退出，服务不停 |

- **调用方的 key**：第一次启动会生成 `service.keys`，一行一个「名字:密钥」，`cat service.keys` 查看。改完 `bash serve.sh restart` 生效。它不进 git。
- **端口**：默认 8788，与文本 judge 的 8787 错开。改端口：`LIQ_JUDGE_PORT=9000 bash serve.sh`。
- **常驻**：脚本已做到关掉 SSH 不退、进程崩溃 5 秒后自动拉起。机器重启后自动恢复需要在 crontab 加一行：
  `*/5 * * * * cd /path/to/voice_agent_v3/liq_judge && bash serve.sh >/dev/null 2>&1`
- **更新**：`git pull && bash serve.sh restart`。
- **网络**：服务器要能直连 `config.env` 里的模型端点，`bash serve.sh check` 会真调一次；对外开放 8788 端口。

---

## 附录：用回放脚本自己生成 trace

没有现成 trace、要对比 copilot 两个版本时，可以用回放脚本把一批会话重放到被测应用上，再整理成 judge 的输入。

```bash
export COPILOT_DIFY_API_KEY=<被测 copilot 应用的 API key>
python3 text_agent/tools/liq_replay.py --cases bench/cases/online_liq_replay_1006.jsonl
```

```bash
python3 liq_judge/cli.py extract bench/runs/<run_id> --cases bench/cases/online_liq_replay_1006.jsonl --gold bench/cases/online_liq_gold_1006.jsonl -o turns.jsonl
```

- 回放时对话历史的时间整体平移到回放时刻，`user` 传真实 uid，工具查的是用户本人的订单。
- 因为时间平移，强平服务按「最近一次」取事件时，常会取到用户原始提问之后才发生的强平。判这类数据时要带上订单真值，或者用原始提问时间作为 `asked_at`。
- 本机访问 Dify 必须走公司 VPN 隧道，脚本会自动找地址为 `10.8.8.x` 的 utun 接口并绑定，找不到会直接报错。
