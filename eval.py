# eval.py — agent2 的自动评测
import json, math, re, sys
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

# ============ T08 oracle：p50/p95/超500占比，与 oracle_t07 并列 ============
METRICS_CSV = SANDBOX / "metrics" / "metrics.csv"
#RQ-5afe7e,967
_RID_URL_METRICS = re.compile(r"(RQ-[0-9a-f]{6},[0-9]+)")
#查询所有的latency，返回一个int列表，列表中每个元素是一个int，表示一个请求的latency
def oracle_t08_latencies() -> list[int]:
    """第 1 步：读 csv，返回 200 个 latency_ms 的列表（顺序无所谓）。
    """
    text = METRICS_CSV.read_text(encoding="utf-8",errors="replace")
    lats = _RID_URL_METRICS.findall(text)
    assert len(lats) == 200, "metrics.csv 里有 200 行，每个 latency_ms 都是 int"
    return [int(lat.split(",")[1]) for lat in lats]

def oracle_t08_nearest_rank(lats: list[int], q: float) -> int:
    """第 2 步：最近秩分位。口径题面已写死：升序排序后取第 ⌈q×N⌉ 个值（1-based），不插值。
      - 禁止插值、禁止取平均：口径就是 nearest-rank，实现和口径不许分叉
      - 执行完之后，先不返回，用命令行独立交叉验证三个数（Git Bash）
        # P95：升序第 190 个（⌈0.95×200⌉=190）
        tail -n +2 sandbox/metrics/metrics.csv | cut -d, -f2 | sort -n | sed -n '190p'
        # P50：第 100 个
        tail -n +2 sandbox/metrics/metrics.csv | cut -d, -f2 | sort -n | sed -n '100p'  
        # 严格 >500 的行数
        awk -F, 'NR>1 && $2>500' sandbox/metrics/metrics.csv | wc -l

    """
    sorted_lats = sorted(lats)
    result = sorted_lats[math.ceil(q * len(lats)) - 1]   # ⌈q×N⌉，1-based；int() 在 q×N 非整除时错一位
    return result

def oracle_t08() -> int:
    """eval 入口：score() 只认一个 int，所以只返回 P95。"""
    lats = oracle_t08_latencies()
    return oracle_t08_nearest_rank(lats, 0.95)

def oracle_t08_p50() -> int:
    """人工核对用，不进打分器。TODO: 复用 nearest_rank，q=0.5"""
    lats = oracle_t08_latencies()
    return oracle_t08_nearest_rank(lats, 0.5)

def oracle_t08_over500() -> tuple[int, int]:
    """人工核对用。TODO:
      - 严格 > 500（csv 里有恰好 500 的行，> 和 >= 会差出好几行）
      - 返回 (行数, 占比百分数取整)，如 (69, 34)
    """
    lats = oracle_t08_latencies()
    over500 = sum(lat > 500 for lat in lats)
    return over500, int(over500 / 200 * 100)


def _std(needle: str, ignore_case: bool = False):
    """工厂：把"要参数的 oracle"固化成"无参可调用"，主循环对 7 条任务一视同仁。
    必须调用它（_std("ERROR")）拿到闭包，传 _std 本身会 TypeError 缺 needle。"""
    return lambda: oracle(needle, ignore_case)



TASKS = [
    # 口径（needle/ignore_case）只活在 _std(...) 闭包里一处；
    # 字段里再存一份就是两个真相源——改了不生效，看着像在起作用。
    {"id": "t01_error_total",   "question": f"统计 batch/ 目录所有 .log 文件中，出现大写 ERROR 的总行数，{_CONTRACT}",   "expected_fn": _std("ERROR")},
    {"id": "t02_lower_error",   "question": f"统计 batch/ 目录所有 .log 文件中，出现小写 error 的总行数，{_CONTRACT}",   "expected_fn": _std("error")},
    {"id": "t03_ci_error",      "question": f"统计 batch/ 目录所有 .log 文件中，不区分大小写出现 error 的总行数，{_CONTRACT}", "expected_fn": _std("error", True)},
    {"id": "t04_warn",          "question": f"统计 batch/ 目录所有 .log 文件中，出现大写 WARN 的总行数，{_CONTRACT}",    "expected_fn": _std("WARN")},
    {"id": "t05_warning_trap",  "question": f"统计 batch/ 目录所有 .log 文件中，出现大写 WARNING 的总行数，{_CONTRACT}",  "expected_fn": _std("WARNING")},
    {"id": "t06_fatal_zero",    "question": f"统计 batch/ 目录所有 .log 文件中，出现大写 FATAL 的总行数，{_CONTRACT}",   "expected_fn": _std("FATAL")},
    {"id": "t07_cross_file",
        "question": ("读 orders/requests.csv（每行一个 URL，request_id 在路径里，形如 /orders/REQ-xxxx）。"
        "扫描 orders/logs/ 下所有 .log，统计仅在 csv 中出现过的那些 request_id 各自出现在多少行；"
        "日志行形如 rid=REQ-xxxx，一行最多一个 rid，日志里其它 rid 一律不计。"
        "按行数降序取前 3（行数相同按 id 升序）。"
        "写入 report.md，倒数第三至最后一行依次为 TOP1=<id>:<行数>、TOP2=...、TOP3=...，"
        "最后一行写 TOTAL=<csv 里全部 id 的总行数>"),
        "expected_fn": oracle_t07},
    {"id": "t08_p95_metrics",
        "question": ("读 metrics/metrics.csv（表头 request_id,latency_ms，共 200 行数据）。"
        "计算 P50 和 P95——口径：最近秩法 nearest-rank，升序排序后取第 ⌈q×200⌉ 个值"
        "（1-based），不插值；再统计延迟严格大于 500ms 的行数与占比（恰好 500ms 不算）。"
        "写入 report.md，依次写 P50=<整数>ms、P95=<整数>ms、OVER=<行数>/<百分比取整>%，"
        "最后一行写 TOTAL=<P95 的毫秒值>"),
        "expected_fn": oracle_t08},
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

    print(f"{'r':<3} {'id':<20} {'model':<18} {'expected':>8} {'got':>6} {'pass':>5}  {'steps':>5} {'calls':>5} {'tokens':>7}")
    print("-" * 90)

    # gots[t_id] = [每轮的 got]，循环结束算 answer_stable
    gots: dict[str, list] = {t["id"]: [] for t in selected[:limit]}

    for r in range(1, repeat + 1):
        for task in selected[:limit]:
            expected = task["expected_fn"]()
            run_dir, report_md, state = run_task(task)
            passed, got = score(report_md, expected)
            gots[task["id"]].append(got)
            print(f"r{r:<2} {task['id']:<20} {MODEL:<18} {expected:>8} {str(got):>6} {'OK' if passed else 'FAIL':>5}  "
                  f"{state['step']:>5} {sum(state['calls'].values()):>5} {state['tokens']:>7}")

            result = {
                "task": {k: v for k, v in task.items() if k != "expected_fn"},
                "round": r,
                "model": MODEL,
                "expected": expected,
                "got": got,
                "passed": passed,
                "run_dir": str(run_dir),
                "state": state,
                "report": report_md,
            }
            # 文件名带轮次：repeat>1 时各轮并存，否则后一轮覆盖前一轮，repeat 就白跑了
            suffix = f"_r{r}" if repeat > 1 else ""
            (runs_dir / f"eval_{task['id']}{suffix}.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    # ── 答案一致性：repeat 的设计目标，跑完必须直接给结论 ──
    if repeat > 1:
        print("-" * 90)
        for tid, gs in gots.items():
            stable = len(set(gs)) == 1
            print(f"{'stable' if stable else 'UNSTABLE':<10} {tid:<20} got: {'/'.join(str(g) for g in gs)}")
