# tools/test_run_python.py — run_python 的离线契约测试（不花钱、不调 API）
# 跑法：python tools/test_run_python.py
# 对应 BACKLOG 阶段 1 分叉裁决后的实现：一次代码执行替代脑内直方图。
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent2

agent2.RUN_DIR = Path("runs/_runpy_test")
agent2.RUN_DIR.mkdir(parents=True, exist_ok=True)
(agent2.RUN_DIR / "artifacts").mkdir(parents=True, exist_ok=True)

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)

# 1. 正常计算：p95 nearest-rank（T08 的核心场景）
r = agent2.run_python(
    "import csv, math\n"
    "lats = sorted(int(row['latency_ms']) for row in csv.DictReader(open('metrics/metrics.csv')))\n"
    "print(lats[math.ceil(0.95*len(lats))-1])\n"
)
check("1.正常算 p95", "1756" in r and r.startswith("[exit 0]"), r[:60])

# 2. 死循环：timeout 生效，工具不挂起
t0 = time.time()
r = agent2.run_python("while True: pass", timeout=2)
elapsed = time.time() - t0
check("2.死循环超时", "[超时]" in r and elapsed < 15, f"{elapsed:.1f}s, {r[:50]}")

# 3. 超大输出：截断且提示明确
r = agent2.run_python("print('x' * 100000)")
check("3.10万字符截断", len(r) <= agent2.RUN_PY_OUTPUT_MAX + 80 and "已截断" in r, f"len={len(r)}")

# 4. 诚实性：它不是沙箱——读沙箱外文件会成功（设计如此，记录在案）
r = agent2.run_python("print(open('../README.md', encoding='utf-8').readline())")
check("4.非沙箱诚实性（../ 可读）", r.startswith("[exit 0]") and "README" not in r and len(r) > 5, r[:50])

# 5. 异常栈回灌：模型拿到栈后可以自愈，工具不抛
r = agent2.run_python("1/0")
check("5.异常栈回灌", "ZeroDivisionError" in r and "[exit" in r, r.splitlines()[-1][:60])

# 6. 空代码：明确报错而不是崩
r = agent2.run_python("   ")
check("6.空代码拒绝", "需要 code 参数" in r)

# 7. 留档：执行的代码存在 artifacts/runpy_*.py
files = sorted((agent2.RUN_DIR / "artifacts").glob("runpy_*.py"))
check("7.代码留档", len(files) >= 5, f"{len(files)} 个文件")

print()
sys.exit(f"{len(fails)} 条失败: {fails}" if fails else print("全部通过"))
