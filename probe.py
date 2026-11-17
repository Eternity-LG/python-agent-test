"""换模型前的契约测试：只验"这个 provider 能不能承接 agent2 的循环"，
不产生 run 目录、不写 state.json、不碰基线。

为什么要它：换 provider 真正的风险不是 token 价格，是 tool calling 的契约差异——
模型名写错直接 404；tools schema 里某个字段服务端不认就 400；
有的模型把调用塞进 content 里返回 0 个 tool_calls，agent2 第一步就判成"结束"；
有的返回 arguments 为空串，json.loads 直接抛异常。

用法：
    python probe.py          # 完整探测
    python probe.py --list   # 只列服务端有哪些模型
"""
import json
import os
import sys

# 注意导入顺序：agent.py 在模块顶层调用 load_env()，所以 .env 在这一行就被补进
# os.environ 了，MODEL / BASE_URL 也是那时定值。probe 自己不用再读一次 .env。
from openai import OpenAI                      # noqa: E402

from agent2 import MODEL, BASE_URL, TOOLS      # noqa: E402


def mask(k: str) -> str:
    return f"{k[:6]}…{k[-4:]}（{len(k)} 位）" if len(k) >= 12 else "（未设置）"


def main() -> int:
    only_list = "--list" in sys.argv
    key = os.getenv("OPENAI_API_KEY", "")

    print(f"base_url : {BASE_URL}")
    print(f"model    : {MODEL}")
    print(f"api_key  : {mask(key)}")
    if not key:
        print('\n[FAIL] 没有 API Key。在项目根建 .env，写一行：OPENAI_API_KEY=sk-xxxxx')
        return 1

    client = OpenAI(api_key=key, base_url=BASE_URL, timeout=60)

    # ---- 1. 模型名是否真实存在：改名/下线是换 provider 最常见的 404 ----
    try:
        ids = [m.id for m in client.models.list().data]
        print(f"\n服务端模型（{len(ids)}）：{', '.join(ids[:12])}")
        if ids and MODEL not in ids:
            print(f"[WARN] MODEL={MODEL!r} 不在列表里，直接调用大概率 404，"
                  f"把 .env 里的 MODEL 换成上面某个 id")
    except Exception as e:
        print(f"[WARN] /models 不可用（{type(e).__name__}: {e}），跳过——有些兼容服务没实现这个端点")
    if only_list:
        return 0

    # ---- 2. 带 tools 的一次真实调用 ----
    print("\n--- 带 tools 的最小调用 ---")
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": "你必须调用工具来完成任务，不要凭记忆回答。"},
                {"role": "user", "content": "用 findstr 统计 sandbox/batch 目录里 error 出现多少行。"},
            ],
            tools=TOOLS,
            temperature=0,
        )
    except Exception as e:
        print(f"[FAIL] 请求被拒：{type(e).__name__}: {e}")
        print("       排查顺序：model 名 → tools schema 是否含服务端不认的字段 → temperature 是否被禁")
        return 1

    choice, msg = resp.choices[0], resp.choices[0].message
    ok = True
    print(f"finish_reason : {choice.finish_reason}")
    print(f"tool_calls    : {len(msg.tool_calls or [])} 个")

    for i, c in enumerate(msg.tool_calls or [], 1):
        raw = c.function.arguments
        try:
            json.loads(raw or "{}")            # agent2 里就是这一句，抛异常整轮就断
            verdict = "合法 JSON"
        except json.JSONDecodeError as e:
            verdict, ok = f"!! 非法 JSON：{e}", False
        print(f"  #{i} name={c.function.name!r} id={c.id!r}")
        print(f"      arguments={raw!r} → {verdict}")

    if not msg.tool_calls:
        ok = False
        print("  !! 返回 0 个 tool_calls，agent2 会在第一步当成'已结束'直接输出文本")
        print(f"     content={(msg.content or '')[:200]!r}")

    # ---- 3. agent2 靠 model_dump 拼历史，字段缺失会 400 ----
    d = {}
    try:
        d = msg.model_dump(exclude_none=True)
        print(f"model_dump    : ok，{len(d)} 字段 {sorted(d)}")
        if "role" not in d:
            ok = False
            print("  !! 缺 role 字段，回填历史会被服务端拒")
    except Exception as e:
        ok = False
        print(f"model_dump    : !! {type(e).__name__}: {e}")

    # ---- 3.5 思考模型：reasoning_content 会不会被原样回传弄成 400 ----
    # 这是只有"换模型"才会暴露的坑：agent2 直接 append msg.model_dump()，
    # 思考内容进了历史，第二轮连同 tool 结果一起发回去。有的 provider 直接拒。
    if d.get("reasoning_content") and msg.tool_calls:
        probe_msgs = [
            {"role": "user", "content": "用 findstr 统计 sandbox/batch 目录里 error 出现多少行。"},
            d,
            {"role": "tool", "tool_call_id": msg.tool_calls[0].id, "content": "共 37 行"},
        ]
        print(f"reasoning     : 是思考模型，{len(d['reasoning_content'])} 字符")
        try:
            client.chat.completions.create(model=MODEL, messages=probe_msgs,
                                           tools=TOOLS, temperature=0)
            print("  第 2 轮回传 ok。副作用：思考文本计入 ctx_chars，"
                  "跨模型比上下文时会虚高，基线表里要注明")
        except Exception as e:
            ok = False
            print(f"  !! 第 2 轮被拒 {type(e).__name__}: {str(e)[:120]}")
            print("     → 在 append 前 pop：d.pop('reasoning_content', None)")

    # ---- 4. usage：agent2 的 tokens 全靠它 ----
    u = resp.usage
    print(f"usage         : prompt={getattr(u, 'prompt_tokens', None)} "
          f"completion={getattr(u, 'completion_tokens', None)} "
          f"total={getattr(u, 'total_tokens', None)}")
    if getattr(u, "total_tokens", None) is None:
        print("  [WARN] 没有 total_tokens，state['tokens'] 会恒为 0，基线表这一列作废")

    print("\n--- 结论 ---")
    if ok:
        print("[PASS] 可以跑 run7 了：python agent2.py")
        return 0
    print("[FAIL] 先清掉上面标 !! 的项再跑。否则 run7 的数据不是'模型差'，是接口没接上。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
