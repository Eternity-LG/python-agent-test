# eval.py — agent2 的自动评测
import json, re, sys
from pathlib import Path

from agent import SANDBOX, MODEL

# ── 1. 任务清单 ──────────────────────────────────
# 每条 question 末尾都有输出契约："报告最后一行写 TOTAL=<数字>"，
# 让打分不依赖自然语言措辞。答案由 oracle 现算，绝不手填。
_CONTRACT = "把结果写成 markdown 保存到 report.md，报告最后一行写 TOTAL=<总行数>"

# ── 2. oracle（纯 Python 现算，绝不手填答案）──────────
def oracle(needle: str, ignore_case: bool = False, pattern: str = "*.log") -> int:
    """统计 SANDBOX/batch/ 下所有匹配文件中，包含 needle 的行数。
    ignore_case=True 时不区分大小写。"""
    total = 0
    target = needle.lower() if ignore_case else needle
    for p in (SANDBOX / "batch").rglob(pattern):
        if not p.is_file():
            continue
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if ignore_case:
                    if target in line.lower():
                        total += 1
                else:
                    if target in line:
                        total += 1
    return total

# ============ T07 oracle：跨文件关联，与 oracle() 并列 ============
CSV_PATH   = SANDBOX / "orders" / "requests.csv"
ORDER_LOGS = SANDBOX / "orders" / "logs"

_RID_URL = re.compile(r"/orders/(REQ-[0-9a-f]{6})")   # csv 里 rid 藏在 URL 路径
_RID_LOG = re.compile(r"rid=(REQ-[0-9a-f]{6})")       # 日志里的形态；干扰 id 同形态，靠 csv 过滤

#---查询所有的req rid
def oracle_t07_ids() -> set[str]:
    """第 1 步：从 csv 提取全部 rid。
    第一行表头是 'url'，不含 /orders/，正则天然过滤，别用行号跳表头。"""
    text = CSV_PATH.read_text(encoding="utf-8", errors="replace")
    return set(_RID_URL.findall(text))

#---遍历所有log文件，统计每个id的命中行数
def oracle_t07_per_id(ids: set[str]) -> dict[str, int]:
    """第 2 步：逐文件逐行，统计每个 id 的命中行数。
    口径：一行最多一个 rid，该行命中的 id 计 1。
    只统计 ids 集合里的——日志里其它 rid 是干扰项，一个都不许进。
    """

    counts = {r:0 for r in ids}
    for f in sorted(ORDER_LOGS.glob("*.log")):
        if not f.is_file():
            continue
        with open(f,"r",encoding="utf-8",errors="replace") as fp:
            for line in fp:
                for rid in _RID_LOG.findall(line):
                    if rid in ids:
                        counts[rid] += 1
    return counts

def oracle_t07() -> int:
    """eval 主循环的入口：只返回 total。score() 只认 int，top3 不进打分器。"""
    counts = oracle_t07_per_id(oracle_t07_ids())
    return sum(counts.values())

def oracle_t07_top3() -> list[tuple[str, int]]:
    """注意：oracle_t07() 和它各自调 per_id 是重复扫描，但 oracle 每任务只跑一次，不值钱。"""
    counts = oracle_t07_per_id(oracle_t07_ids())
    # 按行数、同数按 rid 升序，取前 3
    return sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:3]

#不懂这个函数的作用
def _std(needle: str, ignore_case: bool = False):
    """把现有 oracle 适配成无参 expected_fn，t01-t06 用它，行为一个字不变"""
    return lambda: oracle(needle, ignore_case)

TASKS = [

    {"id": "t01_error_total",   "question": f"统计 batch/ 目录所有 .log 文件中，出现大写 ERROR 的总行数，{_CONTRACT}",   "needle": "ERROR",   "ignore_case": False, "expected_fn": _std("ERROR")},
    {"id": "t02_lower_error",   "question": f"统计 batch/ 目录所有 .log 文件中，出现小写 error 的总行数，{_CONTRACT}",   "needle": "error",   "ignore_case": False, "expected_fn": _std("error")},
    {"id": "t03_ci_error",      "question": f"统计 batch/ 目录所有 .log 文件中，不区分大小写出现 error 的总行数，{_CONTRACT}", "needle": "error", "ignore_case": True, "expected_fn": _std("error", True)},  
    {"id": "t04_warn",          "question": f"统计 batch/ 目录所有 .log 文件中，出现大写 WARN 的总行数，{_CONTRACT}",    "needle": "WARN",    "ignore_case": False, "expected_fn": _std("WARN")}, 
    {"id": "t05_warning_trap",  "question": f"统计 batch/ 目录所有 .log 文件中，出现大写 WARNING 的总行数，{_CONTRACT}",  "needle": "WARNING", "ignore_case": False, "expected_fn": _std("WARNING")},    
    {"id": "t06_fatal_zero",    "question": f"统计 batch/ 目录所有 .log 文件中，出现大写 FATAL 的总行数，{_CONTRACT}",   "needle": "FATAL",   "ignore_case": False, "expected_fn": _std("FATAL")},  
    {"id": "t07_cross_file",
        "question": ("读 orders/requests.csv（每行一个 URL，request_id 在路径里，形如 /orders/REQ-xxxx）。"
        "扫描 orders/logs/ 下所有 .log，统计仅在 csv 中出现过的那些 request_id 各自出现在多少行；"
        "日志行形如 rid=REQ-xxxx，一行最多一个 rid，日志里其它 rid 一律不计。"
        "按行数降序取前 3（行数相同按 id 升序）。"
        "写入 report.md，倒数第三至最后一行依次为 TOP1=<id>:<行数>、TOP2=...、TOP3=...，"
        "最后一行写 TOTAL=<csv 里全部 id 的总行数>"),
        "expected_fn": oracle_t07},
]

# ── 3. 执行（复用 agent2.run，不改它）──────────────
def run_task(task):
    import os
    from agent2 import run
    from openai import OpenAI
    from agent import BASE_URL
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        sys.exit("OPENAI_API_KEY 未设置（检查 .env 是否存在）")
    run_dir, _final = run(task["question"], OpenAI(api_key=key, base_url=BASE_URL))
    try:
        report_text = (run_dir / "report.md").read_text(encoding="utf-8")
    except FileNotFoundError:
        report_text = ""      # 没写报告交给 score 判 got=None，不要中断整轮把前面的花费全废掉
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    return run_dir, report_text, state


# ── 4. 打分（依赖 question 里的 TOTAL= 输出契约）──────
def score(report_md: str, expected: int):
    """返回 (是否通过, 提取到的数字)。got=None 说明 agent 违反了输出契约——
    这本身也算一种失败，但要和"算错数"区分开，所以单独标 None。"""
    m = re.search(r"TOTAL\s*=\s*(\d+)", report_md)
    got = int(m.group(1)) if m else None
    return (got == expected) if got is not None else False, got


# ── 5. 汇总 ──────────────────────────────────────
if __name__ == "__main__":
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else len(TASKS)
    repeat = int(sys.argv[sys.argv.index("--repeat") + 1]) if "--repeat" in sys.argv else 1
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    selected = [t for t in TASKS if only is None or t["id"].startswith(only)]

    runs_dir = Path("runs")
    runs_dir.mkdir(parents=True, exist_ok=True)

    print(f"{'id':<20} {'model':<18} {'expected':>8} {'got':>6} {'pass':>5}  {'steps':>5} {'calls':>5} {'tokens':>7}")
    print("-" * 86)
    for _ in range(repeat):
        for task in selected[:limit]:
        # expected = oracle(task["needle"], task.get("ignore_case", False))
        #不懂这里为什么这么写
            expected = task["expected_fn"]()
            run_dir, report_md, state = run_task(task)
            passed, got = score(report_md, expected)
            print(f"{task['id']:<20} {MODEL:<18} {expected:>8} {str(got):>6} {'OK' if passed else 'FAIL':>5}  "
                  f"{state['step']:>5} {sum(state['calls'].values()):>5} {state['tokens']:>7}")

            result = {
                "task": {k: v for k, v in task.items() if k != "expected_fn"},
                "model": MODEL,
                "expected": expected,
                "got": got,
                "passed": passed,
                "run_dir": str(run_dir),
                "state": state,
                "report": report_md,
            }
            (runs_dir / f"eval_{task['id']}.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )   
