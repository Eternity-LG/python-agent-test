# eval.py — agent2 的自动评测
import json, re, sys
from pathlib import Path

from agent import SANDBOX

# ── 1. 任务清单（答案由 Python 现算，跨平台）──────────
TASKS = [
    {"id": "t01_error_total",   "question": "统计 batch/ 目录所有 .log 文件中，出现大写 ERROR 的总行数，把结果写成 markdown 保存到 report.md",  "needle": "ERROR",   "ignore_case": False},
    {"id": "t02_lower_error",   "question": "...小写 error 的总行数...",                                                        "needle": "error",   "ignore_case": False},
    {"id": "t03_ci_error",      "question": "...不区分大小写 error 的总行数...",                                                  "needle": "error",   "ignore_case": True},
    {"id": "t04_warn",          "question": "...大写 WARN 的总行数...",                                                          "needle": "WARN",    "ignore_case": False},
    {"id": "t05_warning_trap",  "question": "...大写 WARNING 的总行数...",                                                       "needle": "WARNING", "ignore_case": False},
    {"id": "t06_fatal_zero",    "question": "...大写 FATAL 的总行数...",                                                         "needle": "FATAL",   "ignore_case": False},
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
def run_task(task, self_test: bool = True):
    from agent2 import run
    from openai import OpenAI
    from agent import BASE_URL, MODEL
    run_dir, report_md = run(task["question"], OpenAI(base_url=BASE_URL), self_test)
    report_text = (run_dir / "report.md").read_text(encoding="utf-8")
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    return run_dir, report_text, state


# ── 4. 打分 ──────────────────────────────────────
def score(report_md: str, expected: int) -> bool:
    """从 report.md 提取 agent 算出的数字，与 expected 对比。"""
    m = re.search(r"共 (\d+) 行", report_md, re.DOTALL)
    if not m:
        m = re.search(r"总计\s*[:：]?\s*(\d+)", report_md)
    if not m:
        return False
    return int(m.group(1)) == expected


# ── 5. 汇总 ──────────────────────────────────────
if __name__ == "__main__":
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else len(TASKS)

    runs_dir = Path("runs")
    runs_dir.mkdir(parents=True, exist_ok=True)

    print(f"{'id':<20} {'expected':>8} {'pass':>5}")
    print("-" * 38)

    for task in TASKS[:limit]:
        expected = oracle(task["needle"], task.get("ignore_case", False))
        run_dir, report_md, state = run_task(task, self_test=False)
        passed = score(report_md, expected)
        print(f"{task['id']:<20} {expected:>8} {'OK' if passed else 'FAIL':>5}")

        result = {
            "task": task,
            "expected": expected,
            "passed": passed,
            "run_dir": str(run_dir),
            "state": state,
            "report": report_md,
        }
        (runs_dir / f"eval_{task['id']}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
