# tools/test_parallel.py — P3 并行工具执行的离线契约测试
# 跑法：python tools/test_parallel.py
# 测试先行（BACKLOG 阶段 3）：实现写完前，1-5 号预期 AttributeError（红）；
# 6 号测的是已存在的 run_python，现在就应绿。
import sys
import time
import json
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent2

agent2.RUN_DIR = Path("runs/_parallel_test")
agent2.RUN_DIR.mkdir(parents=True, exist_ok=True)
(agent2.RUN_DIR / "artifacts").mkdir(parents=True, exist_ok=True)

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)

# ---------- 测试替身：记录"谁在哪个线程、什么时刻"执行 ----------
DISPATCH_LOG = []          # (thread_id, i)
def fake_dispatch(name, args):
    i = args.get("i", 0)
    DISPATCH_LOG.append((threading.get_ident(), i))
    time.sleep([0.50, 0.40, 0.30, 0.20, 0.10][i % 5])   # 不同耗时 → 顺序会乱，全并行才快
    if i == 999:
        raise ValueError("boom")                          # ④ 单点失败
    return f"result_{i}"

def mk_call(i, name="fake"):
    return SimpleNamespace(
        id=f"call_{i}",
        function=SimpleNamespace(name=name, arguments=json.dumps({"i": i})),
    )

def fresh_state():
    return {"calls": {}, "seen": {}, "step": 1}

# 契约签名：run_calls(tool_calls, state, messages, step) -> (results, step_new)
# messages 是 run() 的局部变量（不在 state 里），由调用方传入，工具消息 append 进它

# 用替身顶替真 dispatch2（模块全局引用，线程池里生效）
agent2.dispatch2 = fake_dispatch

# ---------- ①+② 并行提速 + 按原序回填 ----------
DISPATCH_LOG.clear()
calls = [mk_call(i) for i in range(5)]          # 总耗时 sum=1.5s，最长单个 0.5s
state = fresh_state()
t0 = time.time()
msgs = []
results, step_new = agent2.run_calls(calls, state, msgs, 1)
elapsed = time.time() - t0
check("1.并行提速(墙钟≈max而非sum)", elapsed < 1.2 and len(results) == 5,
      f"{elapsed:.2f}s（串行应≈1.5s）")
check("2.按原序回填", [r for r in results] == [f"result_{i}" for i in range(5)],
      str([r[:9] for r in results]))
check("2b.真的并发了(多线程执行)",
      len({tid for tid, _ in DISPATCH_LOG}) > 1,
      f"{len({tid for tid, _ in DISPATCH_LOG})} 个线程")

# ---------- ④ 单点失败隔离 ----------
DISPATCH_LOG.clear()
calls = [mk_call(0), mk_call(999), mk_call(2)]   # 中间那个会抛
state = fresh_state()
msgs = []
results, step_new = agent2.run_calls(calls, state, msgs, 1)
check("4.单点失败不影响其他",
      len(results) == 3
      and results[0] == "result_0"
      and "工具执行失败" in results[1] and "ValueError" in results[1]
      and results[2] == "result_2",
      str([r[:14] for r in results]))

# ---------- ③ 计数器/指纹/消息配对不丢不乱 ----------
DISPATCH_LOG.clear()
calls = [mk_call(0), mk_call(1), mk_call(0)]     # 0 出现两次 → seen 只记一次新信息
state = fresh_state()
msgs = []
results, step_new = agent2.run_calls(calls, state, msgs, 1)
check("3a.calls 计数恰好=调用数",
      state["calls"] == {'fake|{"i": 0}': 2, 'fake|{"i": 1}': 1},
      json.dumps(state["calls"], ensure_ascii=False))
check("3b.重复指纹只算一次新信息", step_new == 2, f"step_new={step_new}")
check("3c.tool 消息按原序且 id 配对",
      [m["tool_call_id"] for m in msgs] == [c.id for c in calls],
      str([m["tool_call_id"] for m in msgs]))

# ---------- ⑤ 重复调用拦截：计数在主线程做，被拦截的不进线程池 ----------
DISPATCH_LOG.clear()
calls = [mk_call(7)]
state = fresh_state()
state["calls"]['fake|{"i": 7}'] = 4              # 预置成已调 4 次
msgs = []
results, step_new = agent2.run_calls(calls, state, msgs, 1)
check("5.重复调用拦截", "同参数已调用 4 次" in results[0]
      and all(i != 7 for _, i in DISPATCH_LOG), str(results[0][:30]))

# ---------- ⑥ run_python 自身的并发安全（现在就应绿） ----------
# 每次调用用全新目录：_runpy_seq 每进程归零，目录跨进程持久会同名覆盖、污染断言
import shutil
from concurrent.futures import ThreadPoolExecutor
fresh_dir = agent2.RUN_DIR / f"artifacts_fresh_{int(time.time()*1000)}"
fresh_dir.mkdir(parents=True, exist_ok=True)
agent2.RUN_DIR = agent2.RUN_DIR.parent          # run_python 读的是 RUN_DIR，换到全新层
with ThreadPoolExecutor(10) as ex:
    rs = list(ex.map(lambda k: agent2.run_python(f"print({k} * 2)"), range(10)))
new_files = sorted(fresh_dir.glob("runpy_*.py"))
contents = {f.read_text(encoding="utf-8") for f in new_files}
check("6.run_python 10并发:无重名/无覆盖/结果各自正确",
      len(new_files) == 10 and len(contents) == 10
      and all(f"[exit 0]\n{k*2}" in r for k, r in enumerate(rs)),
      f"新增 {len(new_files)} 个文件，内容 {len(contents)} 种")
shutil.rmtree(fresh_dir, ignore_errors=True)

print()
sys.exit(f"{len(fails)} 条失败: {fails}" if fails else print("全部通过"))
