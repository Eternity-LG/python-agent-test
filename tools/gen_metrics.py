# tools/gen_metrics.py — T08 计算型任务数据集生成器
# 200 行延迟数据，长尾分布。seed 固定，重跑结果不变。
# ⚠️ 本文件 stdout 会打印正确答案（P50/P95/OVER）——用户实现 oracle 时禁止照抄，
#    答案只能来自自己的 oracle 与命令行交叉验证的对账。
import random
from pathlib import Path

random.seed(20261119)

OUT = Path(__file__).resolve().parents[1] / "sandbox" / "metrics" / "metrics.csv"
N = 200

lats: list[int] = []
lats += [random.randint(120, 420) for _ in range(117)]    # 常规流量
lats += [random.randint(430, 1100) for _ in range(45)]    # 中速
lats += [random.randint(1100, 2000) for _ in range(18)]   # 慢尾部
lats += [873] * 8                                          # 重复值（同值多行）
lats += [500] * 7                                          # 恰好 500：> 与 >= 的口径陷阱
lats += [random.randint(5000, 9000) for _ in range(5)]     # 离群点（长尾）
assert len(lats) == N, len(lats)
random.shuffle(lats)

rids = [f"RQ-{random.randbytes(3).hex()}" for _ in range(N)]
OUT.parent.mkdir(parents=True, exist_ok=True)
with OUT.open("w", encoding="utf-8", newline="\n") as f:
    f.write("request_id,latency_ms\n")
    for rid, ms in zip(rids, lats):
        f.write(f"{rid},{ms}\n")

# ---- 生成器自检（答案区，勿抄）----
s = sorted(lats)
ceil = lambda x: -(-x // 1)
import math
p50 = s[math.ceil(0.50 * N) - 1]
p95 = s[math.ceil(0.95 * N) - 1]
over = sum(1 for v in lats if v > 500)
print(f"rows={N}  P50={p50}ms  P95={p95}ms  OVER={over} ({over/N*100:.0f}%)")
print(f"outliers>4s: {sorted(v for v in lats if v > 4000)}")
print(f"恰好500的行数: {sum(1 for v in lats if v == 500)}")
