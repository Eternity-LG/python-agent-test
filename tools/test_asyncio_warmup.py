# tools/test_asyncio_warmup.py — 5.0 asyncio 最小集的离线契约测试
# 跑法：python tools/test_asyncio_warmup.py
# 被测对象：tools/asyncio_warmup.py（三个函数，均为离线替身，不花 API 钱）
# 测试先行（BACKLOG 阶段 5.0）：实现写完前 1-4 号预期红；5 号是语义演示，永远绿。
import sys
import time
import asyncio
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tools.asyncio_warmup as warm

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)

# ---------- 测试替身：假的异步工具，记录执行时刻 ----------
async def fake_atool(i, delay):
    await asyncio.sleep(delay)
    return f"result_{i}"

def sync_tool(name, args):
    """同步工具替身：记录线程 id，睡指定时长（模拟 findstr 的文件 IO）"""
    i = args.get("i", 0)
    delay = args.get("delay", 0.0)
    SYNC_LOG.append((threading.get_ident(), i))
    time.sleep(delay)
    return f"sync_{i}"

SYNC_LOG = []   # (thread_id, i)

# ---------- ① 协程心智模型：调用不执行，await 才执行 ----------
def test1():
    coro = warm.fake_llm("hi", 0.01)
    # 调用返回协程对象，await 才执行
    is_coro = asyncio.iscoroutine(coro)
    result = None
    if is_coro:
        async def driver():
            return await coro
            # 调用返回协程对象，await 才执行
        result = asyncio.run(driver())
    check("1.协程心智模型(调用返回协程对象,await 才产出结果)",
          is_coro and result == "hi",
          f"iscoroutine={is_coro}, result={result!r}")
    if is_coro and result is None:
        coro.close()   # 防止未消费协程的 RuntimeWarning

# ---------- ② gather 并发：墙钟 ≈ max 而非 sum ----------
def test2():
    coros = [fake_atool(i, d) for i, d in
             enumerate([0.50, 0.40, 0.30, 0.20, 0.10])]
    t0 = time.perf_counter()
    results = asyncio.run(warm.gather_all(coros))
    wall = time.perf_counter() - t0
    ok = (results == [f"result_{i}" for i in range(5)]
          and wall < 1.0)          # 串行 1.5s；全并发 ≈0.5s
    check("2.gather 并发(墙钟≈max 0.5s 而非 sum 1.5s)",
          ok, f"{wall:.2f}s, results={results}")

# ---------- ③ 原序回填：完成顺序乱，结果顺序不乱 ----------
def test3():
    # 完成顺序必然是 2,1,0（耗时递减），但回填必须 0,1,2 —— 对应 P3 的 tool_call_id 配对
    coros = [fake_atool(i, d) for i, d in enumerate([0.30, 0.20, 0.10])]
    t0 = time.perf_counter()
    results = asyncio.run(warm.gather_all(coros))
    wall = time.perf_counter() - t0
    check("3.原序回填(完成序 2,1,0 但结果保持输入序 0,1,2)",
          results == ["result_0", "result_1", "result_2"] and wall < 0.8,
          f"{wall:.2f}s, results={results}")

# ---------- ④ 同步进异步：to_thread 不阻塞事件循环 ----------
def test4():
    main_tid = threading.get_ident()
    async def driver():
        coros = [warm.to_thread_tool(sync_tool, "findstr", {"i": i, "delay": d})
                 for i, d in enumerate([0.40, 0.30, 0.20, 0.10, 0.05])]
        t0 = time.perf_counter()
        results = await warm.gather_all(coros)
        wall = time.perf_counter() - t0
        return results, wall
    results, wall = asyncio.run(driver())
    tids = {t for t, _ in SYNC_LOG}
    ok = (results == [f"sync_{i}" for i in range(5)]
          and wall < 0.9                    # 串行 1.05s；to_thread 并发 ≈0.4s
          and main_tid not in tids          # 没有阻塞主线程（事件循环线程）
          and len(tids) >= 1)
    check("4.同步函数经 to_thread 进并发(墙钟≈max,事件循环不被阻塞)",
          ok, f"{wall:.2f}s, 线程数={len(tids)}, results={results}")

# ---------- ⑤ 语义演示：await 让出后，共享状态会被别的协程改掉（无需实现） ----------
def test5():
    counter = {"v": 0}
    async def bump():
        v = counter["v"]          # 读
        await asyncio.sleep(0.05)  # 让出事件循环——另一协程趁虚而入
        counter["v"] = v + 1      # 写回旧值：丢失更新
    async def demo():
        await asyncio.gather(bump(), bump())
        return counter["v"]
    lost = asyncio.run(demo())
    # 这是 async 的关键差异：单线程无锁竞争，但 await 是切换点。
    # 并行工具的 state 计数若在 await 后写回，会像这样丢更新——
    # 对照 P3 的教训：所有共享状态修改必须在 await 之外一口气完成。
    check("5.[演示]await 让出导致丢失更新(2 次自增只落 1 次)",
          lost == 1, f"counter={lost}(期望串行下是 2)")

if __name__ == "__main__":
    for t in (test1, test2, test3, test4, test5):
        t()
    print()
    if fails:
        print(f"{len(fails)} 条失败: {fails}")
        sys.exit(1)
    print("全部通过")
