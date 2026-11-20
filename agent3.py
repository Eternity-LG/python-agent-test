"""agent3.py — LangGraph 对照迁移（BACKLOG 5.1）
对照实验纪律：只换调度层。TOOLS/dispatch2/findstr/run_python/SYSTEM/MODEL 全部
复用 agent2，一行不改——这样五列对比（tokens/调用/准确率/行数/调试穿透层数）
里的一切差异都能归因到"调度层的表达方式"。

TODO 标记处是你的作业（5 处）；其余是脚手架。
手写 loop 与图的本质差异（先读再写）：
  - 手写：你亲手 while 循环 + 顺序判断 + 原地改 state
  - 图：node 只返回"增量 dict"，怎么合并进全局 state 由 reducer 声明；
        分支是显式的条件边；循环由图的拓扑自己走
"""
import asyncio
import json
import operator
import time
import uuid
from pathlib import Path
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages

import agent2
from agent2 import (TOOLS, MODEL, BASE_URL, SYSTEM, MAX_STEPS,
                    ARTIFACT_THRESHOLD, safe_dispatch, offload, ctx_chars, log)


# ---------- ① State 定义：本阶段的核心考点是 reducer ----------
# LangGraph 里 node 返回增量，框架按每个字段的 reducer 决定怎么合并：
#   无 reducer 字段 → 覆盖；有 reducer 字段 → reducer(旧值, 增量)
# 对照手写版：state["calls"][key] += 1 是你原地改的；这里必须表达成
# "tools_node 返回 {key: 1} 增量，由 reducer 累加"。

def merge_calls(old: dict, new: dict) -> dict:
    """TODO 1：calls 是"参数指纹 -> 次数"。合并规则 = 按 key 累加。
    提示：out = dict(old) 起底，逐 key out[k] = out.get(k, 0) + v。"""
    raise NotImplementedError("TODO 1")


def keep_first(old: dict, new: dict) -> dict:
    """TODO 2：seen 是"指纹 -> 首次出现的 step"。合并规则 = 旧值优先——
    重复出现的指纹不该更新首次时间戳，否则 no_progress 判定会被污染。
    想一想它和 TODO 1 为什么不同：同一个"合并 dict"的语义，两种正确答案。"""
    raise NotImplementedError("TODO 2")


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]   # 框架内置：消息按追加合并
    calls: Annotated[dict, merge_calls]
    seen: Annotated[dict, keep_first]
    tokens: Annotated[int, operator.add]      # 框架内置：数值累加
    step: Annotated[int, operator.add]
    no_progress: int                          # 无 reducer：node 返回什么就是什么
    tool_seq: int                             # 同上：offload 计数器，覆盖语义


# ---------- 脚手架：消息格式桥（框架"隐藏 message 结构"的第一现场） ----------
# LangGraph 的 add_messages 会把 dict 转成 LangChain 消息对象；而 OpenAI SDK
# 只吃纯 dict。每次进 LLM 前要转回来——这个摩擦正是验收清单要的"新约束"。
def _to_openai_dicts(messages) -> list[dict]:
    out = []
    for m in messages:
        if isinstance(m, dict):
            out.append(m)
        elif isinstance(m, ToolMessage):
            out.append({"role": "tool", "tool_call_id": m.tool_call_id,
                        "content": m.content})
        elif isinstance(m, AIMessage):
            d = {"role": "assistant", "content": m.content}
            if m.tool_calls:
                d["tool_calls"] = [{"id": tc["id"], "type": "function",
                                    "function": {"name": tc["name"],
                                                 "arguments": json.dumps(tc["args"], ensure_ascii=False)}}
                                   for tc in m.tool_calls]
            if not d["content"] and not d.get("tool_calls"):
                d["content"] = None
            out.append(d)
        elif isinstance(m, SystemMessage):
            out.append({"role": "system", "content": m.content})
        elif isinstance(m, HumanMessage):
            out.append({"role": "user", "content": m.content})
        else:
            raise TypeError(f"未知消息类型: {type(m)}")
    return out


# ---------- ② llm_node ----------
def make_llm_node(client):
    def llm_node(state: AgentState) -> dict:
        """TODO 3：对照 agent2.run 的 LLM 调用段（750-761 行）。

        要做的事：
        - resp = client.chat.completions.create(model=MODEL,
              messages=_to_openai_dicts(state["messages"]), tools=TOOLS, temperature=0)
        - d = resp.choices[0].message.model_dump(exclude_none=True)
          然后 pop reasoning_content 落盘（不 pop 它会以 additional_kwargs 身份
          混回下一轮请求——手写版 pop 的理由在这里同样成立）
        - 返回增量：{"messages": [d], "tokens": resp.usage.total_tokens, "step": 1}
          注意：messages 里放的是"这条新消息"，不是整个历史——追加由 add_messages 干
        - 步数上限不用判：图的 recursion_limit 管（见 run() 的 config）

        坑位预告：tools_node 里拿到的 tool_calls 是 LangChain 已解析好的
        [{"name", "args"(dict), "id"}]——不再是 OpenAI 原始 JSON 字符串。
        """
        raise NotImplementedError("TODO 3")
    return llm_node


# ---------- ③ tools_node（5.0 热身成果的主战场） ----------
async def tools_node(state: AgentState) -> dict:
    """TODO 4：对照 agent2.run_calls 的三段式，全部翻译成"增量返回"。

    - last = state["messages"][-1]；last.tool_calls 即上面说的已解析列表
    - 重复拦截：**不许 state["calls"][key] += 1 原地改**！
      算增量：对每个 call，key 构造法照抄 run_calls 的 Phase A（name|sorted json）；
      预判次数 = state["calls"].get(key, 0) + 1；>=4 的用 preset 文本替代执行、
      不进并发池；返回 {"calls": {每个执行或预判的 key: 1}} 让 reducer 累加
    - 并发执行：asyncio.to_thread(safe_dispatch, name, args) 包每个真实 call，
      然后 gather_all（你在 tools/asyncio_warmup.py 写的那个）并发跑——
      gather 返回顺序 = 输入顺序，P3 的"原序回填"这里免费
    - offload：结果 > ARTIFACT_THRESHOLD 且工具名 != read_artifact 时，
      tool_seq 计数递增并 offload（照抄 run_calls Phase C）。
      想清楚 tool_seq 怎么返回：它是覆盖字段，返回 {"tool_seq": 新值}
    - no_progress：整步零新信息（指纹全部已见）则 +1，有新信息归 0；
      连续 >=2 时往 messages 增量里 append 那条"原地打转"user 提示后归 0
      （文案照抄 agent2.run 776-778 行）
    - seen 增量：{fp: state["step"]} 只对首次出现的指纹
    - 返回 {"messages": [tool 消息...（含可能的灌提示）], "calls": ..., "seen": ...,
            "no_progress": 新值, "tool_seq": 新值}
    """
    raise NotImplementedError("TODO 4")


# ---------- ④ 条件边 ----------
def route(state: AgentState) -> str:
    """TODO 5：最后一条消息带 tool_calls → "tools"，否则 → END。
    对照手写版的 if not msg.tool_calls 分支——手写是顺序判断，图是把分支画出来。"""
    raise NotImplementedError("TODO 5")


# ---------- ⑤ 组图（脚手架，不用改） ----------
def build_graph(client):
    g = StateGraph(AgentState)
    g.add_node("llm", make_llm_node(client))
    g.add_node("tools", tools_node)
    g.add_edge(START, "llm")
    g.add_conditional_edges("llm", route)
    g.add_edge("tools", "llm")
    return g.compile()


# ---------- ⑥ run 入口（脚手架雏形；TODO 6 你补 config） ----------
def run(question: str, client, self_test: bool = False, resume: str | None = None) -> tuple[Path, str]:
    """签名与 agent2.run 完全一致——eval.py 将来只需换 import 就能 A/B 交错跑。
    checkpoint/断点续跑由 LangGraph 的 checkpointer 管（这是"框架真解决了什么"
    的候选证据）；本骨架先不接 checkpointer，跑到对照后再补。"""
    base = agent2.RUNS / "_selftest" if self_test else agent2.RUNS
    agent2.RUN_DIR = base / (time.strftime("%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4])
    (agent2.RUN_DIR / "artifacts").mkdir(parents=True)
    log(event="start", question=question, model=MODEL, base_url=BASE_URL, engine="langgraph")
    print(f"[run] {agent2.RUN_DIR.name}")

    graph = build_graph(client)
    init = {"messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": question}],
            "calls": {}, "seen": {}, "tokens": 0, "step": 0,
            "no_progress": 0, "tool_seq": 0}
    # TODO 6：手写版的 MAX_STEPS=40 在图里对应 config 的 recursion_limit。
    # 想清楚倍数关系：图的"一步"是 node 粒度（llm + tools 各算一次），
    # 该填多少才能和手写版同口径？（答案写在本行注释里再填数）
    config = {"recursion_limit": None}          # ← None 换成你算出的数
    final = asyncio.run(graph.ainvoke(init, config=config))

    answer = final["messages"][-1].content
    tokens = final.get("tokens", 0)
    print(f"[finish] step={final['step']} tokens={tokens}")
    log(event="finish", step=final["step"], tokens=tokens, engine="langgraph")
    return agent2.RUN_DIR, answer or ""
