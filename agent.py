"""最小可运行的 agent loop —— 不用任何 agent 框架，只有 openai SDK。

用法:
    pip install -r requirements.txt
    export OPENAI_API_KEY=sk-xxx        # Windows cmd 用 set OPENAI_API_KEY=sk-xxx
    python agent.py                     # 连接真实模型
    python agent.py --self-test         # 没有 key 也能跑，验证循环逻辑

换模型只改环境变量，代码一行不动:
    OPENAI_BASE_URL / OPENAI_API_KEY / MODEL
"""
import ast
import json
import operator
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from openai import OpenAI

from envfile import load_env

load_env()              # 必须在下面几行之前：MODEL / BASE_URL 是导入即定值的常量

BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
MODEL = os.getenv("MODEL", "deepseek-v4-flash")
MAX_STEPS = 12          # 硬性终止条件。没有它，agent 能一直循环到烧完额度
MAX_TOOL_CHARS = 4000   # 单个工具结果回灌上限。上下文爆炸全是从这里开始的
READ_CHAR_BUDGET = 1000 # read_file 的字符预算。必须低于 agent2.ARTIFACT_THRESHOLD(1200)，
                        # 否则结果落盘、模型还得再调 read_artifact，一次读原文付两次钱
SANDBOX = Path(os.getenv("AGENT_SANDBOX", Path(__file__).parent / "sandbox")).resolve()

# ---------- 0. 系统提示词 ----------
# 你是一个会使用工具的助手。需要信息时先调用工具，不要凭空猜测；
# 拿到足够信息后直接给出结论，并在结论里说明每个数字来自哪个文件。
SYSTEM = (
    "你是一个会使用工具的助手。需要信息时先调用工具，不要凭空猜测；"
    "拿到足够信息后直接给出结论，并在结论里说明每个数字来自哪个文件。"
)

# ---------- 1. 工具声明：这段 schema 本身也是 prompt 的一部分 ----------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出目录下的文件名，不递归。不知道有哪些文件时必须先调用它。",
            "parameters": {
                "type": "object",
                "properties": {"subdir": {"type": "string", "description": "相对路径，根目录用 \".\""}},
                "required": ["subdir"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "按行读取文本文件。输出同时受行数和字符预算（约 1000 字符）限制，截断时会说明给了第几到第几行、以及下一次该传的 offset。它适合看原文片段；只统计或定位某段内容时用 findstr 的 show_lines 更省——只返回命中行，不被预算挤占",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径，如 app.log"},
                    "max_lines": {"type": "integer", "description": "本次最多读多少行，默认 200；字符预算先到会先截断"},
                    "offset": {"type": "integer", "description": "从第几行开始读（0 表示文件开头）。截断提示里会给出下一次该传的值"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calc",
            "description": "四则运算求值，用于统计求和。只接受数字和 + - * / % **。",
            "parameters": {
                "type": "object",
                "properties": {"expression": {"type": "string", "description": "如 3+2"}},
                "required": ["expression"],
            },
        },
    },
]


# ---------- 2. 工具实现：每个工具都是一个有副作用边界的函数 ----------
def _safe(path: str) -> Path:
    """沙箱校验。工具越权访问是 agent 最常见的安全事故，这一行必须写。"""
    p = (SANDBOX / path).resolve()
    if SANDBOX not in p.parents and p != SANDBOX:
        raise PermissionError(f"路径越界，只允许访问 {SANDBOX.name}/ 内部: {path}")
    return p

# ---------- 2.1. 列出目录下的文件名 ----------
def list_files(subdir: str = ".") -> str:
    p = _safe(subdir)
    items = sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir())
    return "\n".join(items) if items else "(空目录)"

# ---------- 2.2. 读取文本文件的前若干行 ----------
def read_file(path: str, max_lines: int = 200, offset: int = 0,
              max_chars: int = READ_CHAR_BUDGET) -> str:
    """按行读取文件，同时受行数和字符预算两个上限约束，支持按 offset 分段。

    为什么要字符预算：只限行数时，30 行的日志就有 1500+ 字符，超过
    agent2.ARTIFACT_THRESHOLD 会被落盘，模型想看内容还得再调一次
    read_artifact —— 一次读原文付两次钱（实测 t05 就是这么浪费的）。
    把预算压到阈值以下，read_file 一次就能把"给得起的信息"全给完。

    为什么要 offset：预算一旦成为硬闸门，没有 offset 就等于文件尾部永远读不到
    （加 max_lines 也越不过预算）。截断提示里直接给出下一次该传的 offset。

    截断时明确交代：给了第几到第几行 / 一共几行 / 下一次从哪读。
    模型不该靠猜自己拿到的是不是全文。
    """
    lines = _safe(path).read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(0, int(offset))
    if start >= len(lines) and lines:
        return (f"offset={start} 超出文件行数：{path} 共 {len(lines)} 行"
                f"（行号从 1 开始，offset 从 0 开始，最大可传 {len(lines) - 1}）")
    budget = max(200, min(int(max_chars), READ_CHAR_BUDGET))   # 硬顶：传大也无效
    window = lines[start:start + max(1, max_lines)]
    picked, used = [], 0
    for line in window:
        cost = len(line) + 1                                   # +1 是换行符
        if used + cost > budget:
            break
        picked.append(line)
        used += cost
    cut_line = False
    if not picked and window:           # 首行就超预算：硬切一刀，别返回空
        picked.append(window[0][:max(1, budget - 140)])
        cut_line = True
    body = "\n".join(picked)
    stop = start + len(picked)
    if cut_line:                        # 单行超预算，交代"这一行也没给全"
        body += (f"\n...(已截断：第 {start + 1} 行超过 {budget} 字符预算，只给了前 "
                 f"{len(picked[0])} 字符；文件共 {len(lines)} 行。"
                 f"要定位具体内容请用 findstr 的 show_lines)")
    elif stop < len(lines):
        body += (f"\n...(已截断：本次给了第 {start + 1}–{stop} 行，共 {len(lines)} 行，"
                 f"受 {budget} 字符预算限制。要看后续请传 offset={stop}，"
                 f"或改用 findstr 的 show_lines 只取命中行)")
    return body

# ---------- 2.3. 四则运算求值 ----------
# 递归求值表达式树，支持 + - * / % ** 运算符。
_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
    ast.USub: operator.neg,
}
# ---------- 2.3.1. 递归求值表达式树 ----------
def _ev(node):
    if isinstance(node, ast.Expression):
        return _ev(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp):
        return _OPS[type(node.op)](_ev(node.left), _ev(node.right))
    if isinstance(node, ast.UnaryOp):
        return _OPS[type(node.op)](_ev(node.operand))
    raise ValueError("只支持数字与四则运算")


def calc(expression: str) -> str:
    return str(_ev(ast.parse(expression, mode="eval")))

# ---------- 2.3.2. 工具注册 ----------
REGISTRY = {"list_files": list_files, "read_file": read_file, "calc": calc}

# ---------- 2.3.3. 工具调用 ----------
def dispatch(name: str, args: dict) -> str:
    if name not in REGISTRY:
        raise ValueError(f"未知工具: {name}")
    return REGISTRY[name](**args)


# ---------- 3. 核心循环：这就是 agent 的全部 ----------
def run(question: str, client) -> str:
    # ---------- 3.1. 初始化消息 ----------
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": question},
    ]
    #
    calls: dict = {}
    total_tokens = 0

    # ---------- 3.2. 循环调用模型 ----------
    for step in range(1, MAX_STEPS + 1):
        resp = client.chat.completions.create(
            model=MODEL, messages=messages, tools=TOOLS, temperature=0
        )
        total_tokens += resp.usage.total_tokens if resp.usage else 0
        msg = resp.choices[0].message

        # 关键：assistant 这条消息必须原样回填，尤其是 tool_calls。
        # 少了它，下一轮模型会报 "tool messages must follow tool_calls"。
        messages.append(msg.model_dump(exclude_none=True))

        if not msg.tool_calls:          # 终止条件只有一个：模型不再要工具
            print(f"[step {step}] 最终答案，累计 {total_tokens} tokens")
            return msg.content or ""

        for call in msg.tool_calls:
            #这里进行反序列化是为了，再次通过序列化对参数进行排序，确保参数顺序不同导致缓存键不同，漏重
            
            # arguments 是 JSON 字符串而不是 dict，需要先反序列化；
            # 模型偶尔会传空串，兜底成 "{}" 避免 json.loads 抛异常
            args = json.loads(call.function.arguments or "{}")

            # 构造「去重指纹」：字典不可哈希、不能直接当 dict 的 key，
            # 所以先序列化成字符串，再与工具名组合成元组（同为工具名+同参数 = 同一次调用）
            # sort_keys=True    按 key 排序，避免参数顺序不同导致指纹不同而漏掉重复
            # ensure_ascii=False 中文原样保留，不转成 \uXXXX，便于日志排查
            key = (call.function.name, json.dumps(args, sort_keys=True, ensure_ascii=False))

            #calls.get(key, 0) 获取当前键的调用次数，默认值为 0 ，如果键不存在则返回 0
            #calls[key] = calls.get(key, 0) + 1 更新当前键的调用次数，加 1 后赋值给 calls[key]
            #如果键不存在，则创建一个新键，赋值为 1 ,calls[key]是列表，每个元素是一个调用记录
            calls[key] = calls.get(key, 0) + 1

            if calls[key] >= 3:
                result = "同样参数已调用过 3 次，结果不会变。请换参数或直接给结论。"
                print(f"[step {step}] {call.function.name} 重复调用，已拦截")
            else:
                try:
                    result = dispatch(call.function.name, args)
                    print(f"[step {step}] {call.function.name}({args}) -> {len(str(result))} 字符")
                except Exception as e:
                    # 错误回灌而不是抛出去。agent 能不能自愈，全看这一步。
                    result = f"工具执行失败 {type(e).__name__}: {e}"
                    print(f"[step {step}] {call.function.name} 报错: {e}")

            messages.append({
                "role": "tool",
                "tool_call_id": call.id,            # 必须对上 call.id，否则 400
                "content": str(result)[:MAX_TOOL_CHARS],
            })

    print("[warn] 达到最大步数，强制终止")
    return "未在限定步数内完成，已中止。"


# ---------- 4. 自检模式：用脚本化的假模型验证循环，不消耗额度 ----------
class _Fn:
    def __init__(self, name, args):
        self.name, self.arguments = name, json.dumps(args, ensure_ascii=False)


class _Call:
    def __init__(self, i, name, args):
        self.id, self.type, self.function = f"call_{i}", "function", _Fn(name, args)


class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.role, self.content, self.tool_calls = "assistant", content, tool_calls

    def model_dump(self, exclude_none=False):
        d = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [
                {"id": c.id, "type": "function",
                 "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in self.tool_calls
            ]
        return d


class FakeClient:
    def __init__(self, script):
        self.script = list[Any](script)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        msg = self.script.pop(0)
        choice = SimpleNamespace(message=msg, finish_reason="tool_calls" if msg.tool_calls else "stop")
        return SimpleNamespace(choices=[choice], usage=SimpleNamespace(total_tokens=0))


FAKE = [
    _Msg(tool_calls=[_Call(1, "list_files", {"subdir": "."})]),
    _Msg(tool_calls=[_Call(2, "read_file", {"path": "app.log"}),
                     _Call(3, "read_file", {"path": "api.log"})]),
    _Msg(tool_calls=[_Call(4, "read_file", {"path": "nope.log"})]),   # 故意触发错误回灌
    _Msg(content="app.log 3 条，api.log 2 条，worker.log 0 条，合计 5 条 ERROR。"),
]

QUESTION = "统计 sandbox 下所有 .log 文件里的 ERROR 行数，按文件列出来并给出总数。"

if __name__ == "__main__":
    if "--self-test" in sys.argv:
        print("=== 自检模式：假模型验证循环，不花额度 ===\n")
        print(run(QUESTION, FakeClient(FAKE)))
    else:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            sys.exit("请先设置 OPENAI_API_KEY；没有 key 可以先跑 python agent.py --self-test")
        print(f"model={MODEL}  base_url={BASE_URL}  sandbox={SANDBOX}\n")
        print(run(QUESTION, OpenAI(api_key=api_key, base_url=BASE_URL)))
