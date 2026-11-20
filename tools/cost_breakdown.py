# tools/cost_breakdown.py — 成本归因：把标称 tokens 拆成四块可归因的构成
# 用法: python tools/cost_breakdown.py [runs/xxxx-id]   （留空取最新 run）
# 只读 trace.jsonl，不写任何文件（它要进 eval 后处理链，不许留意外产物）。
#
# 语义说明（本工具唯一的知识门槛）：
#   trace.jsonl 里 event=step 的行，两种口径混在一起——
#   - tokens      : 累计值（state 逐步累加）→ 做差才是本步消耗
#   - prompt / completion / reasoning / cache_hit / cache_miss
#                 : 当次请求的 usage      → 直接用，做差无意义
#   自检: Δtokens ≈ prompt + completion（对不上说明 trace 格式变了，先怀疑自己）
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 相对单价（P2b 否决时确认：cache_hit ≈ miss 的 1/10）。
# TODO: 对照 DeepSeek 定价页核实三个倍率，价格填对，
# "标称 tokens"与"真实账单"的差值就是本工具最大的产出。
PRICES = {"hit": 0.1, "miss": 1.0, "completion": 2.0}


def find_trace(arg: str | None) -> Path:
    """定位 trace.jsonl：给 run 目录/名就用它，缺省取 runs/ 下最新的数字开头目录。"""
    runs = ROOT / "runs"
    if arg:
        p = Path(arg)
        if not p.exists():
            p = runs / arg                      # 只给 run 名时拼 runs/ 前缀
        trace = p / "trace.jsonl" if p.is_dir() else p
    else:
        dests = sorted((d for d in runs.iterdir()
                        if d.is_dir() and d.name.split("-")[0].isdigit()),
                       key=lambda d: d.name, reverse=True)
        if not dests:
            raise FileNotFoundError(f"runs/ 下没有 run 目录: {runs}")
        trace = dests[0] / "trace.jsonl"
    if not trace.exists():
        raise FileNotFoundError(f"trace.jsonl 不存在: {trace}")
    return trace


def load_steps(trace: Path) -> tuple[list[dict], dict | None]:
    """返回 (step 事件列表, finish 事件或 None)。

    最后一步若直接给出最终答案，走 finish 而非 step——它的 tokens 消耗
    也必须计入（T08 111k run 里这一步恰好是最后一个 +25k reasoning 爆炸）。
    """
    steps, finish = [], None
    with open(trace, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                d = json.loads(line)
                if d.get("event") == "step":
                    steps.append(d)
                elif d.get("event") == "finish":
                    finish = d
    if not steps:
        raise ValueError(f"trace 里没有 step 事件: {trace}")
    return steps, finish


def breakdown(steps: list[dict]) -> list[dict]:
    """核心算术：每步拆成 重发历史 / 新增 / completion(含 reasoning) / 真实相对成本。

    - 重发历史_i ≈ prompt_{i-1}（上一步的完整 prompt 原样重发；首步为 0）
    - 新增_i     = prompt_i - prompt_{i-1}（本步 append 的 assistant+tool 内容）
    - completion / reasoning / cache_* 是当次值，直接取
    - 真实相对成本 = hit×P_hit + miss×P_miss + completion×P_completion
    """
    rows, prev_prompt, prev_tokens = [], 0, 0
    for s in steps:
        prompt = s.get("prompt") or 0
        comp = s.get("completion") or 0
        hit = s.get("cache_hit") or 0           # 旧 run 无此列 → None → 0（当 miss 算）
        miss = s.get("cache_miss") or 0
        dtokens = s["tokens"] - prev_tokens
        # 自检：Δtokens ≈ prompt + completion（容差 1% 给四舍五入）
        if abs(dtokens - (prompt + comp)) > max(10, dtokens * 0.01):
            print(f"[warn] step{s['step']} 自检失败: Δtokens={dtokens} "
                  f"≠ prompt({prompt})+completion({comp})，trace 口径可能变了")
        rows.append({
            "step": s["step"],
            "ms": s.get("ms") or 0,
            "dtokens": dtokens,
            "prompt": prompt,
            "hit": hit,
            "miss": miss,
            "resent": prev_prompt,              # 重发历史（标称）
            "new": prompt - prev_prompt,        # 新增内容
            "comp": comp,
            "reas": s.get("reasoning") or 0,
            "cost": hit * PRICES["hit"] + miss * PRICES["miss"]
                    + comp * PRICES["completion"],
        })
        prev_prompt, prev_tokens = prompt, s["tokens"]
    return rows


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None   # argv[0] 是脚本名，参数从 1 起
    trace = find_trace(arg)
    steps, finish = load_steps(trace)

    # legacy 降级：usage 三列上线（commit 961bf47）之前的 run 没有 prompt/completion，
    # 但 artifacts/ 留了 reasoning_stepN.txt——用 Δtokens 归因 + reasoning 字符数佐证
    legacy = all(not s.get("prompt") for s in steps)
    if legacy:
        print(f"[legacy] 该 run 早于 usage 三列（commit 961bf47），只做 Δtokens 归因，"
              f"reasoning 用 artifacts 字符数佐证（chars≈tokens×1.6 粗估）\n")
        rows = []
        prev_tokens = 0
        evs = steps + ([finish] if finish and finish.get("tokens") else [])
        for s in evs:
            dt = s["tokens"] - prev_tokens
            rf = trace.parent / "artifacts" / f"reasoning_step{s['step']}.txt"
            rea_chars = rf.stat().st_size if rf.exists() else None
            rows.append({"step": s["step"], "ms": s.get("ms") or 0, "dtokens": dt,
                         "rea_chars": rea_chars, "is_finish": s is finish})
            prev_tokens = s["tokens"]
        print(f"{'step':>4} {'ms':>7} {'Δtokens':>8}   reasoning 文件(chars)")
        for r in rows:
            rea = f"{r['rea_chars']:>8} (~{r['rea_chars']//1.6:>6.0f} tok)" \
                  if r["rea_chars"] else "-"
            mark = " (finish)" if r["is_finish"] else ""
            print(f"{r['step']:>4} {r['ms']:>7} {r['dtokens']:>8}   {rea}{mark}")
        tot = sum(r["dtokens"] for r in rows)
        rea_tok = sum((r["rea_chars"] or 0) / 1.6 for r in rows)
        top2 = sorted(rows, key=lambda r: r["dtokens"], reverse=True)[:2]
        print("-" * 60)
        print(f"{'合计':>4} {'':>7} {tot:>8}   reasoning 估 {rea_tok:.0f} tok"
              f"（占 {rea_tok/tot:.0%}）")
        print(f"       钱最多的两步: "
              + ", ".join(f"step{r['step']}（Δ{r['dtokens']}）" for r in top2))
        return

    rows = breakdown(steps)
    # finish 事件的收尾消耗（最后一步直接出答案时走 finish 不走 step）
    if finish and finish.get("tokens"):
        prev = steps[-1]["tokens"]
        if finish["tokens"] > prev:
            f = {"step": f"{finish['step']}*", "ms": None, "dtokens": finish["tokens"] - prev,
                 "prompt": None, "hit": None, "miss": None,
                 "resent": None, "new": None, "comp": None, "reas": None, "cost": 0}
            rows.append(f)
            print(f"[note] step{finish['step']} 走的是 finish 事件（直接出答案），"
                  f"无 usage 细分，只计入 Δtokens={f['dtokens']}")

    print(f"{'step':>4} {'ms':>7} {'Δtokens':>8} {'prompt':>7} "
          f"{'hit/miss':>11} {'重发':>6} {'新增':>6} {'comp(rea)':>12} {'相对成本':>8}")
    for r in rows:
        # finish 合成行没有 usage 细分，各列显示 "-"
        ms_s = "-" if r["ms"] is None else str(r["ms"])
        pr_s = "-" if r["prompt"] is None else str(r["prompt"])
        hm_s = "-" if r["hit"] is None else f"{r['hit']}/{r['miss']}"
        re_s = "-" if r["resent"] is None else str(r["resent"])
        nw_s = "-" if r["new"] is None else str(r["new"])
        cp_s = "-" if r["comp"] is None else f"{r['comp']}({r['reas']})"
        print(f"{str(r['step']):>4} {ms_s:>7} {r['dtokens']:>8} {pr_s:>7} "
              f"{hm_s:>11} {re_s:>6} {nw_s:>6} {cp_s:>12} {r['cost']:>8.0f}")

    tot_t = sum(r["dtokens"] for r in rows)
    # finish 合成行无 usage 细分，聚合时按 None→0 兜底（dtokens 仍计入）
    g = lambda k: sum(r[k] or 0 for r in rows)
    tot_resent, tot_new = g("resent"), g("new")
    tot_comp, tot_reas = g("comp"), g("reas")
    tot_cost = sum(r["cost"] for r in rows)
    resent_cost = sum((r["hit"] or 0) * PRICES["hit"]
                      + max((r["resent"] or 0) - (r["hit"] or 0), 0) * PRICES["miss"]
                      for r in rows)
    print("-" * 74)
    den = tot_resent + tot_new or 1
    print(f"{'合计':>4} {'':>7} {tot_t:>8} {'':>7} {'':>11} {tot_resent:>6} {tot_new:>6} "
          f"{tot_comp:>6}({tot_reas:<5}) {tot_cost:>8.0f}")
    print(f"{'占比':>4}   Δtokens 构成: 重发 {tot_resent/den:>4.0%} / 新增 {tot_new/den:>4.0%}；"
          f"completion 占 Δtokens {tot_comp/tot_t:>4.0%}（其中 reasoning {tot_reas/tot_t:.0%}）")
    print(f"       真实账单（相对）: {tot_cost:.0f} / 标称 tokens {tot_t} → "
          f"标称值是真实成本的 {tot_t/tot_cost if tot_cost else 0:.1f} 倍；"
          f"其中重发历史折价后仅占 {resent_cost/tot_cost if tot_cost else 0:.0%}")
    top2 = sorted(rows, key=lambda r: r["cost"], reverse=True)[:2]
    print(f"       钱最多的两步: "
          + ", ".join(f"step{r['step']}（相对成本 {r['cost']:.0f}）" for r in top2))


if __name__ == "__main__":
    main()
