# tools/test_compress.py — compress_old_tools 的离线契约测试
# 不花钱、不调 API。跑法：python tools/test_compress.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # 让 import agent2 生效
import agent2

if not hasattr(agent2, "compress_old_tools"):
    # P2a 已按负结果关闭（代码在 agent2.py 注释保留，见 BASELINE.md「P2a」）
    # 本测试随之失效；若重启压缩，先解开 agent2.py 里的注释再跑本文件
    sys.exit("compress_old_tools 不存在：P2a 已关闭，测试跳过（非失败）。重启压缩后可复跑。")

RUN = Path("runs/_comp_test")
agent2.RUN_DIR = RUN
(RUN / "artifacts").mkdir(parents=True, exist_ok=True)
for p in (RUN / "artifacts").glob("toolmsg_*"):
    p.unlink()          # 清掉上次运行的产物，保证断言可重复


def make_msgs(n_tools: int) -> list:
    """构造 n_tools 对 (assistant带tool_calls, tool返回) 的消息历史"""
    msgs = [{"role": "user", "content": "q"}]
    for i in range(n_tools):
        msgs.append({"role": "assistant", "content": "",
                     "tool_calls": [{"id": f"c{i}", "type": "function",
                                     "function": {"name": "findstr", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}",
                     "content": f"目录 batch 下共 {i} 行\n明细节"})
    return msgs


# 断言 1：5 条 tool -> 压 2 条（保留最近 3 条）
msgs = make_msgs(5)
n = agent2.compress_old_tools(msgs, RUN)
assert n == 2, f"压缩条数应为 2，实际 {n}"

# 断言 2：落盘文件名互不相同（计数器自增，不互相覆盖）
files = sorted(p.name for p in (RUN / "artifacts").glob("toolmsg_*"))
assert files == ["toolmsg_1.txt", "toolmsg_2.txt"], f"应有两个不同名文件，实际 {files}"

# 断言 3：被压的 content 变成摘要，含裸名（read_artifact 只认裸名）和原文首行
assert msgs[2]["content"].startswith("[已压缩]"), msgs[2]["content"]
assert "toolmsg_1" in msgs[2]["content"], msgs[2]["content"]
assert "目录 batch 下共 0 行" in msgs[2]["content"], "摘要应携带原文首行"

# 断言 4：最近 3 条原文未动（c2/c3/c4 保留，被压的是 c0/c1）
assert msgs[6]["content"] == "目录 batch 下共 2 行\n明细节"
assert msgs[10]["content"] == "目录 batch 下共 4 行\n明细节"

# 断言 5：幂等——对已压缩的历史再跑一遍，不产生新压缩
n2 = agent2.compress_old_tools(msgs, RUN)
assert n2 == 0, f"幂等失败：第二次又压缩了 {n2} 条"

# 断言 6：边界——4 条压 1 条，3 条压 0 条
assert agent2.compress_old_tools(make_msgs(4), RUN) == 1
assert agent2.compress_old_tools(make_msgs(3), RUN) == 0

# 断言 7：逃生通道真实可用——按摘要里的裸名取回原文
back = agent2.read_artifact("toolmsg_1")
assert "目录 batch 下共 0 行" in back, back[:100]

print("7 条断言全部通过")
print("被压消息示例:", msgs[2]["content"][:110])
