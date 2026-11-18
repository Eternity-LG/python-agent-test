"""生成 T07 数据：sandbox/orders/{requests.csv, logs/log_01..12.log}

为什么生成器放在 tools/ 而不是 sandbox/：
agent 的沙箱根就是 sandbox/，它能 list_files + read_file 看见里面的一切。
生成器里写着每个 id 的目标行数，等于把答案直接交给模型。

重跑会覆盖同一批文件（SEED 固定），用于以后调整规模。改 SEED 就是一批新数据。
"""
from __future__ import annotations

import random
from pathlib import Path

SEED = 20261118
ROOT = Path(__file__).resolve().parents[1] / "sandbox" / "orders"
LOG_DIR = ROOT / "logs"
N_FILES = 12
LINES_PER_FILE = 50

rnd = random.Random(SEED)


def make_rid() -> str:
    return "REQ-" + "".join(rnd.choice("0123456789abcdef") for _ in range(6))


# 30 个真实 id：前三个是 top3 候选，分布刻意不均（有 1 行也有 38 行）
ids = [make_rid() for _ in range(30)]
counts = [38, 27, 19, 12, 11, 9, 8, 7, 6, 6,
          5, 5, 4, 4, 3, 3, 2, 2, 2, 1,
          1, 1, 1, 1, 1, 1, 1, 0, 0, 1]
target = dict(zip(ids, counts))

# 干扰：日志里有、csv 里没有的 rid。只按"统计所有 rid"做的实现会多算这部分
noise_ids = [make_rid() for _ in range(3)]
noise_counts = [8, 7, 6]


def ts(i: int) -> str:
    sec = i * rnd.randint(2, 4)
    return f"2026-11-18 {10 + sec // 3600:02d}:{(sec // 60) % 60:02d}:{sec % 60:02d}.{rnd.randint(0, 999):03d}"


def hit_line(r: str) -> str:
    lvl = rnd.choice(["INFO", "INFO", "INFO", "WARN", "ERROR"])
    return f"{lvl:<5} rid={r} status={rnd.choice(['ok', 'ok', 'retry'])} latency={rnd.randint(5, 900)}ms"


def plain_line() -> str:
    return rnd.choice([
        "INFO  GET /health 200 3ms",
        f"DEBUG cache hit key=user:{rnd.randint(1000, 9999)}",
        "INFO  GET /v1/cart 200 12ms",
        f"WARN  slow query {rnd.randint(300, 1200)}ms table=orders",
        "INFO  POST /v1/checkout 201 45ms",
    ])


rows: list[str] = []
for rid, n in target.items():
    rows += [hit_line(rid)] * n
for rid, n in zip(noise_ids, noise_counts):
    rows += [hit_line(rid)] * n

total_lines = N_FILES * LINES_PER_FILE
rows += [plain_line() for _ in range(total_lines - len(rows))]
rnd.shuffle(rows)

LOG_DIR.mkdir(parents=True, exist_ok=True)
for f in LOG_DIR.glob("*.log"):
    f.unlink()
for i in range(N_FILES):
    chunk = rows[i * LINES_PER_FILE:(i + 1) * LINES_PER_FILE]
    body = "\n".join(f"{ts(i * LINES_PER_FILE + j)} {line}" for j, line in enumerate(chunk))
    (LOG_DIR / f"log_{i + 1:02d}.log").write_text(body + "\n", encoding="utf-8")

# csv 只给 URL，rid 要从路径里提取（这一步是"多跳"的来源）
shuffled = ids[:]
rnd.shuffle(shuffled)
csv_lines = ["url"] + [
    f"https://api.shop.example.com/v1/orders/{r}?page={rnd.randint(1, 3)}&size=20"
    for r in shuffled
]
(ROOT / "requests.csv").write_text("\n".join(csv_lines) + "\n", encoding="utf-8")

# ---------- 自检：重新扫一遍文件核对，别信上面那个列表 ----------
seen: dict[str, int] = {}
for f in sorted(LOG_DIR.glob("*.log")):
    for line in f.read_text(encoding="utf-8").splitlines():
        for token in line.split():
            if token.startswith("rid="):
                r = token[4:]
                seen[r] = seen.get(r, 0) + 1

bad = {r: (target[r], seen.get(r, 0)) for r in target if target[r] != seen.get(r, 0)}
assert not bad, f"生成与目标不一致: {bad}"

got_total = sum(seen.get(r, 0) for r in target)
top3 = sorted(((seen.get(r, 0), r) for r in target), key=lambda x: (-x[0], x[1]))[:3]

print(f"目录      : {ROOT}")
print(f"日志      : {N_FILES} 个文件 × {LINES_PER_FILE} 行 = {total_lines} 行")
print(f"csv       : {len(ids)} 个 id（rid 在 URL 路径里）")
print(f"csv 内行数: {got_total}")
print(f"零命中 id : {sum(1 for r in target if target[r] == 0)} 个")
print(f"干扰 id   : {len(noise_ids)} 个，共 {sum(noise_counts)} 行（不在 csv 里）")
print("top3      : " + ", ".join(f"{r}:{c}" for c, r in top3))
print(f"单文件大小: {min(f.stat().st_size for f in LOG_DIR.glob('*.log'))}–"
      f"{max(f.stat().st_size for f in LOG_DIR.glob('*.log'))} 字节")
