# tools/test_agent3.py — 5.1 LangGraph 调度层的离线冒烟测试
# 跑法：python tools/test_agent3.py
# 测试先行：TODO 1-6 实现完成前全红（NotImplementedError）。
# 假 client 按剧本回两轮：①带 tool_calls 的 assistant（调真 run_python）
# ②最终回答 "done"——验证图接线/reducer/工具复用，不花 API 钱。
import sys
import asyncio
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent2
import agent3

# 假 RUN_DIR：run_python 的代码留档和 offload 都写这里，别污染真实 runs/
TEST_RUN = Path(__file__).resolve().parents[1] / "runs" / "_agent3_test"
(agent2.RUNS).mkdir(parents=True, exist_ok=True)
agent2.RUN_DIR = TEST_RUN
(agent2.RUN_DIR / "artifacts").mkdir(parents=True, exist_ok=True)

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)

# ---------- 测试替身：按剧本回话的假 client ----------
class FakeMsg:
    def __init__(self, d):
        self._d = d
        self.content = d.get("content")
        self.tool_calls = d.get("tool_calls")
    def model_dump(self, exclude_none=True):
        return {k: v for k, v in self._d.items() if not (exclude_none and v is None)}

def mk_resp(tool_calls=None, content=None, usage=(10, 5)):
    d = {"role": "assistant", "content": content}
    if tool_calls:
        d["tool_calls"] = tool_calls
    return SimpleNamespace(
        choices=[SimpleNamespace(message=FakeMsg(d))],
        usage=SimpleNamespace(
            prompt_tokens=usage[0], completion_tokens=usage[1],
            total_tokens=usage[0] + usage[1],
            completion_tokens_details=None,
            prompt_cache_hit_tokens=None, prompt_cache_miss_tokens=None),
    )

class FakeClient:
    def __init__(self, resps):
        self.resps = list(resps)
        self.i = 0
        self.chat = self
        self.completions = self
    def create(self, **kw):
        self.last_kwargs = kw
        r = self.resps[self.i]
        self.i += 1
        return r

SCRIPT = [
    mk_resp(tool_calls=[{"id": "call_1", "type": "function",
                         "function": {"name": "run_python",
                                      "arguments": "{\"code\": \"print('hi')\"}"}}],
            usage=(10, 5)),
    mk_resp(content="done", usage=(20, 10)),
]

# ---------- 跑图 ----------
def run_graph():
    client = FakeClient(SCRIPT)
    graph = agent3.build_graph(client)
    init = {"messages": [{"role": "system", "content": "sys"},
                         {"role": "user", "content": "q"}],
            "calls": {}, "seen": {}, "tokens": 0, "step": 0,
            "no_progress": 0, "tool_seq": 0}
    return asyncio.run(graph.ainvoke(init, config={"recursion_limit": 20}))

try:
    final = run_graph()
except NotImplementedError as e:
    print(f"全部红：实现未完成 —— {e}")
    sys.exit(1)
except Exception as e:
    print(f"非预期异常（可能是实现 bug，贴回话术里排查）：{type(e).__name__}: {e}")
    raise

msgs = final["messages"]
check("1.图接线正确：剧本两轮走完，最终回答是 done",
      msgs[-1].content == "done", f"last={msgs[-1].content!r}")
tool_msgs = [m for m in msgs
             if getattr(m, "type", None) == "tool"
             or (isinstance(m, dict) and m.get("role") == "tool")]
check("2.工具复用未改：run_python 真执行了（tool 消息含 [exit 0] 和 hi）",
      len(tool_msgs) == 1 and "[exit 0]" in str(tool_msgs[0].content)
      and "hi" in str(tool_msgs[0].content)
      and str(tool_msgs[0].content).strip().endswith("hi"),
      f"tool 消息数={len(tool_msgs)}, content[:60]={str(tool_msgs[0].content)[:60]!r}" if tool_msgs else "无 tool 消息")
check("3.tokens reducer 累加：15+30=45（不是覆盖成 30）",
      final["tokens"] == 45, f"tokens={final['tokens']}")
check("4.step reducer 累加：两次 llm_node = 2",
      final["step"] == 2, f"step={final['step']}")
calls = final["calls"]
check("5.calls reducer 累加：run_python 指纹计数=1",
      len(calls) == 1 and list(calls.values())[0] == 1
      and "run_python" in list(calls.keys())[0],
      f"calls={ {k[:40]: v for k, v in calls.items()} }")
check("6.tool_call_id 配对：tool 消息的 id=call_1",
      bool(tool_msgs) and getattr(tool_msgs[0], "tool_call_id", None) == "call_1",
      f"id={getattr(tool_msgs[0], 'tool_call_id', None) if tool_msgs else None}")
check("7.传给 LLM 的是纯 dict：消息里无 LangChain 对象残留",
      all(isinstance(m, dict) for m in client.last_kwargs["messages"]),
      f"types={[type(m).__name__ for m in client.last_kwargs['messages']]}")

print()
if fails:
    print(f"{len(fails)} 条失败: {fails}")
    sys.exit(1)
print("全部通过")
