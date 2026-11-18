# eval.py — agent2 的自动评测
import json, re, sys
from pathlib import Path

from agent import SANDBOX

# ── 1. 任务清单 ──────────────────────────────────
# 每条 question 末尾都有输出契约："报告最后一行写 TOTAL=<数字>"，
# 让打分不依赖自然语言措辞。答案由 oracle 现算，绝不手填。
_CONTRACT = "把结果写成 markdown 保存到 report.md，报告最后一行写 TOTAL=<总行数>"

TASKS = [
    {"id": "t01_error_total",   "question": f"统计 batch/ 目录所有 .log 文件中，出现大写 ERROR 的总行数，{_CONTRACT}",   "needle": "ERROR",   "ignore_case": False},
    {"id": "t02_lower_error",   "question": f"统计 batch/ 目录所有 .log 文件中，出现小写 error 的总行数，{_CONTRACT}",   "needle": "error",   "ignore_case": False},
    {"id": "t03_ci_error",      "question": f"统计 batch/ 目录所有 .log 文件中，不区分大小写出现 error 的总行数，{_CONTRACT}", "needle": "error", "ignore_case": True},
    {"id": "t04_warn",          "question": f"统计 batch/ 目录所有 .log 文件中，出现大写 WARN 的总行数，{_CONTRACT}",    "needle": "WARN",    "ignore_case": False},
    {"id": "t05_warning_trap",  "question": f"统计 batch/ 目录所有 .log 文件中，出现大写 WARNING 的总行数，{_CONTRACT}",  "needle": "WARNING", "ignore_case": False},
    {"id": "t06_fatal_zero",    "question": f"统计 batch/ 目录所有 .log 文件中，出现大写 FATAL 的总行数，{_CONTRACT}",   "needle": "FATAL",   "ignore_case": False},
]


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
    report_text = (run_dir / "report.md").read_text(encoding="utf-8")
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

    runs_dir = Path("runs")
    runs_dir.mkdir(parents=True, exist_ok=True)

    print(f"{'id':<20} {'expected':>8} {'got':>6} {'pass':>5}  {'steps':>5} {'calls':>5} {'tokens':>7}")
    print("-" * 68)

    for task in TASKS[:limit]:
        expected = oracle(task["needle"], task.get("ignore_case", False))
        run_dir, report_md, state = run_task(task)
        passed, got = score(report_md, expected)
        print(f"{task['id']:<20} {expected:>8} {str(got):>6} {'OK' if passed else 'FAIL':>5}  "
              f"{state['step']:>5} {sum(state['calls'].values()):>5} {state['tokens']:>7}")

        result = {
            "task": task,
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
