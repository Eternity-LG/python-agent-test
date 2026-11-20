"""asyncio 热身（BACKLOG 5.0）—— 三个函数对应三个学习点，全是离线替身"""
import asyncio

# TODO 1：协程是"可暂停的函数"
# async def fake_llm(reply: str, delay: float) -> str:
#     睡 delay 秒，返回 reply。
#     注意：睡眠必须用 await asyncio.sleep(delay)，不是 time.sleep——
#     后者会卡死事件循环，测试 4 就在防这个。
async def fake_llm(reply: str, delay: float) -> str:
    await asyncio.sleep(delay)
    return reply

# TODO 2：并发跑一批协程（对应 P3 一次响应多个 tool_call）
# async def gather_all(coros) -> list:
#     一行核心：return await asyncio.gather(*coros)
#     gather 返回顺序天然 = 传入顺序，这就是它适合回填 tool 消息的原因。
#     （对照 P3：你在那边用 submit+字典手动对齐，这里语义免费送）
async def gather_all(coros) -> list:
    return await asyncio.gather(*coros)



# TODO 3：同步函数进 async 世界（5.1 迁移时每个同步工具都要过这道门）
# async def to_thread_tool(sync_fn, name: str, args: dict) -> str:
#     核心一行：return await asyncio.to_thread(sync_fn, name, args)
#     to_thread 的返回值是协程，所以要 await 它——两层 await 是常态，别慌。
async def to_thread_tool(sync_fn, name: str, args: dict) -> str:
    return await asyncio.to_thread(sync_fn, name, args)
