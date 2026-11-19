"""v2：长任务版 agent —— 相比 agent.py 只多了三件事。

1. trace.jsonl   每步的耗时、上下文大小、token、工具调用全部留痕
2. 大结果外置     超过阈值的工具返回值落成文件，messages 里只放摘要 + 引用
3. checkpoint    每步结束原子写 state.json，进程挂了能 --resume 接着跑

用法:
    python agent2.py                          # 新任务
    python agent2.py --resume 1116-1920-a1b2  # 从断点继续
    python agent2.py --self-test              # 不花额度，验证落盘与恢复逻辑
"""
import json
import os
import sys
import time
import uuid
import hashlib, re
from pathlib import Path

from openai import OpenAI

from agent import BASE_URL, MODEL, SANDBOX, TOOLS as BASE_TOOLS, dispatch

MAX_STEPS = 40          # 任务变长了，步数上限必须放宽——这也是上下文会爆的原因
ARTIFACT_THRESHOLD = 1200   # 工具结果超过这么多字符就落盘，只把摘要塞回 messages
                            # （read_file 的字符预算 agent.READ_CHAR_BUDGET=1000 要低于它，
                            #   否则读原文会落盘、模型还得再调 read_artifact，付两次钱）

_runs_env = os.getenv("RUNS")           # .env 里的 RUNS 优先，否则用项目根/runs
if _runs_env:
    _p = Path(_runs_env).expanduser()
    # 相对路径按项目根解析，不跟着 cwd 跑——否则从别的目录启动会把 runs 建到那里
    RUNS = _p if _p.is_absolute() else (Path(__file__).parent / _p).resolve()
else:
    RUNS = Path(__file__).parent / "runs"
RUN_DIR: Path = None    # 当前运行的目录，被 read_artifact / write_report 共享
from envfile import load_env

load_env()      # 幂等兜底：真正定值的是上面 import agent 时 agent.py 顶部那次
SYSTEM = (
    "你是数据分析助手。规则：\n"
    "1. 需要文件信息时先调用工具，不要猜测文件名。\n"
    "2. 同一个文件不要重复读取；读完立刻把结论记下来再处理下一个。\n"
    "3. 判断依据严格按用户给出的定义，不要自行放宽。\n"
    "4. 全部处理完后，用 write_report 写报告，再给出总数。\n"
    "5. 报告最后一行写 TOTAL=<数字>。\n"
)

QUESTION = (
    "读 batch/ 下所有 .log 文件，统计**每个文件**里出现大写 ERROR 的行数。"
    "只统计大写 ERROR，小写 error 不算。"
    "把结果写成 markdown 表格（列：文件名 | ERROR 行数）保存到 report.md，"
    "最后在回答里给出所有文件的 ERROR 总数。"
)


# ---------- 新增工具，计算内容重复率 ----------
# 每次都会变、但不携带信息的片段：落盘名、分页头、时间戳
_VOLATILE = re.compile(r"tool_\d+|第 \d+–\d+ 行|\d{4}-\d\d-\d\d \d\d:\d\d:\d\d")

#  指纹计算
def fingerprint(result: str) -> str:
    s = _VOLATILE.sub("#", str(result))[:300]
    return f"{len(s) // 50}|{hashlib.md5(s.encode('utf-8')).hexdigest()[:8]}"

# ---------- 新增工具：读外置结果 / 写报告 ----------
def _artifact(name: str) -> Path:
    p = (RUN_DIR / "artifacts" / f"{name}.txt").resolve()
    if RUN_DIR.resolve() not in p.parents:
        raise PermissionError(f"非法 artifact 名: {name}")
    return p

# 读取外置结果
def read_artifact(name: str, offset: int = 0, limit: int = 200) -> str:
    lines = _artifact(name).read_text(encoding="utf-8", errors="replace").splitlines()
    chunk = lines[offset: offset + limit]
    head = f"[artifacts/{name}.txt 共 {len(lines)} 行，本次返回第 {offset}–{offset + len(chunk) - 1} 行]\n"
    return head + "\n".join(chunk)

# 写报告
def write_report(path: str, content: str) -> str:
    p = (RUN_DIR / path).resolve()
    if RUN_DIR.resolve() not in p.parents:
        raise PermissionError(f"只能写入本次运行目录: {path}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"已写入 {p.name}，{len(content)} 字符"

# ---------- 新增工具：findstr ----------
def _zero_evidence(p_root: Path, needle: str, single: bool, ignore_case: bool = False) -> str:
    """零值自证：回答"这个 0 是真的没有，还是没读到"。
    只在 total == 0 时调用——正常有命中的路径不付这个成本。
    给三样东西：分母（扫了多少文件/行）、最可能的误判原因（大小写变体）、一条可核对的样例。"""
    files = [p_root] if single else sorted(p for p in p_root.rglob("*.*") if p.is_file())
    n_lines, variants = 0, []
    low = needle.lower()
    for f in files:
        rel = f.name if single else f.relative_to(p_root).as_posix()
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh, 1):
                    n_lines += 1
                    # 含 needle 但大小写不同：这才是模型看到 0 时第一个怀疑的对象
                    if needle not in line and low in line.lower():
                        variants.append((rel, i, line.rstrip("\r\n")))
        except OSError:
            continue
    head = f"已扫描 {len(files)} 个文件、{n_lines} 行（读完整了，不是没找到文件）"
    if not variants:
        how = "忽略大小写地搜" if ignore_case else "区分大小写地搜"
        return f"{head}；{how} {needle!r} 确实一处都没有"
    rel, no, text = variants[0]
    return (f"{head}；另有 {len(variants)} 行是大小写不同的写法，"
            f"如 {rel}:{no} {text[:70]!r}。若要忽略大小写请改传 needle={low!r}")


_ZERO_SCAN_MAX = 32     # 零命中文件逐个数行的上限，防止大目录退化成全量重扫


def _zero_files_note(p_root: Path, zero_names: list[str], needle: str) -> str:
    """目录模式下"部分文件 0 命中"时，给这些 0 也补上分母。

    run7 的教训：全目录总数 37 不为 0，于是 log_02.log 那个单独的 0 拿不到任何凭据，
    模型只能改用 read_file 读 1402 字符全文去确认它是真的没有。0 的可信度不该取决于
    它是不是恰好等于全目录总数——任何一个 0 都要能被追问"你扫完了吗"。
    """
    if len(zero_names) > _ZERO_SCAN_MAX:
        head = "、".join(zero_names[:3])
        return (f"另 {len(zero_names)} 个文件零命中（{head} 等），文件过多未逐个核对行数；"
                f"可对可疑文件传 show_lines=true 单独确认")

    low = needle.lower()
    n_lines, hit = 0, None
    for name in zero_names:
        try:
            with open(p_root / name, "r", encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh, 1):
                    n_lines += 1
                    if hit is None and needle not in line and low in line.lower():
                        hit = (name, i, line.rstrip("\r\n"))
        except OSError:
            continue
    note = (f"另 {len(zero_names)} 个文件零命中（{'、'.join(zero_names)}），"
            f"共 {n_lines} 行已完整扫描")
    if hit:
        name, no, text = hit
        note += (f"；其中 {name}:{no} 是大小写不同的写法 {text[:60]!r}，"
                 f"要核对请传 needle={low!r}")
    return note

MAX_NEEDLES = 40      # 40 × 约 22 字符 ≈ 880，仍在 1200 落盘阈值之下
def findstr(path: str, needle: str="", show_lines: bool = False, max_lines: int = 12,
            ignore_case: bool = False, needles: list[str]|None = None) -> str:
    """统计 path 目录（含子目录）或单个文件中，包含 needle 的行数。
    show_lines=True 时额外返回匹配行，格式为“文件名:行号: 内容”，最多返回 max_lines 行。
    ignore_case=True 时不区分大小写——统计 ERROR+error 总行数应传 true，
    而不是分别搜两遍再相加（t03 的两次实测：不加这个参数模型会裸读全部文件，
    两次分别烧掉 188k 和 167k tokens）。
    path 指向单个文件时走纯 Python 扫描（子进程的 cwd 不能是文件）。"""
    from agent import _safe

    needle = needle or ""          # null / 空值归一

    # ---- 归一化：needles 优先；去重保序；超上限截断并明说 ----
    if isinstance(needles, str):
        needles = [needles]
    raw = [str(n).strip() for n in (needles or []) if str(n).strip()]

    if not raw and needle.strip():
        raw = [needle.strip()]
    if not raw:
        return "findstr 需要 needle 或 needles 至少给一个非空字符串"

    items, seen = [], set()
    for n in raw:
        if n not in seen:
            seen.add(n)
            items.append(n)
    cut_note = ""
    if len(items) > MAX_NEEDLES:
        cut_note = f"\n注意：只统计了前 {MAX_NEEDLES} 个，另有 {len(items) - MAX_NEEDLES} 个未统计"
        items = items[:MAX_NEEDLES]

    try:
        p_root = _safe(path)  # 先做沙箱校验，再交给 shell
        if not p_root.exists():
            return (f"{path} 不存在。请先用 list_files 列出目录，"
                    f"确认文件名拼写（沙箱根目录: sandbox/）")
        single = p_root.is_file()
        if single:
            all_files = [p_root.name]
        else:
            all_files = sorted(
                p.relative_to(p_root).as_posix()
                for p in p_root.rglob("*.*") if p.is_file()
            )
        if not all_files:
            return f"{path} 是空目录：没有任何文件。请确认路径，或用 list_files 查看目录内容"

        if len(items) > 1:
            files = [p_root] if single else sorted(p for p in p_root.rglob("*.*") if p.is_file())
            return _findstr_many(path, files, items, ignore_case) + cut_note

        needle = items[0]

        if show_lines:
            matches = (_findstr_lines_single(p_root, needle, ignore_case) if single
                       else _findstr_lines(p_root, needle, ignore_case))
            # 工具自己守住输出预算：单行实测约 84 字符，12 行约 1100 < 1200 落盘阈值。
            # max_lines 来自模型，不可信任，超出预算一律收紧——否则输出被落盘，
            # show_lines 就失去了"替代 read_file→read_artifact 三连"的意义。
            limit = min(max(1, max_lines), 12)
            total = len(matches)
            if total == 0:
                scope = f"文件 {path}" if single else f"{path} 下 {len(all_files)} 个文件"
                case_note = "（区分大小写）" if not ignore_case else "（已忽略大小写）"
                return (f"{scope} 中未找到 {needle!r}{case_note}\n"
                        f"{_zero_evidence(p_root, needle, single, ignore_case)}")
            detail = "\n".join(
                f"{name}:{lineno}: {content}"
                for name, lineno, content in matches[:limit]
            )
            header = f"{path} 中 {needle!r} 共 {total} 行匹配，返回前 {min(total, limit)} 行"
            if total > limit:
                header += f"（其余 {total - limit} 行未列出，可把 path 收窄到单个文件）"
            return f"{header}\n{detail}"

        if single:
            n = len(_findstr_lines_single(p_root, needle, ignore_case))
            if n == 0:
                case_note = "" if ignore_case else "（区分大小写）"
                return (f"文件 {path} 中 {needle!r} 共 0 行{case_note}\n"
                        f"{_zero_evidence(p_root, needle, True, ignore_case)}")
            return f"文件 {path} 中 {needle!r} 共 {n} 行"

        # findstr/grep 不返回 0 匹配的文件；shell 不可用时回退到 Python
        counts = _findstr_subprocess(p_root, needle, ignore_case)
        if counts is None:
            counts = _findstr_python(p_root, needle, ignore_case) or {}
        counts = {name.replace("\\", "/"): count for name, count in counts.items()}
        per_file = {name: counts.get(name, 0) for name in all_files}
        total = sum(per_file.values())
        case_note = "（不区分大小写）" if ignore_case else ""
        detail = "\n".join(f"  {name}: {count}" for name, count in per_file.items())
        body = (f"目录 {path} 下 {len(per_file)} 个文件中，"
                f"{needle!r} 共出现在 {total} 行{case_note}\n{detail}")
        if total == 0:
            body += "\n" + _zero_evidence(p_root, needle, False, ignore_case)
        else:
            zero_names = [name for name, count in per_file.items() if count == 0]
            if zero_names:
                body += "\n" + _zero_files_note(p_root, zero_names, needle)
        return body
    except PermissionError as e:
        return (f"findstr 越权拒绝 {path}：{e}。"
                f"只能传 sandbox/ 内的相对路径，如 batch 或 batch/log_01.log")
    except FileNotFoundError:
        return f"findstr 找不到路径 {path}。请先用 list_files 确认文件名"
    except Exception as e:
        return f"findstr 执行失败 {type(e).__name__}: {e}"

def _findstr_lines_single(p: Path, needle: str, ignore_case: bool = False) -> list[tuple[str, int, str]]:
    """扫描单个文件，返回 [(文件名, 行号, 行内容)]。
    刻意不走子进程：subprocess 的 cwd 传一个文件会抛 NotADirectoryError，
    而单文件本来就很小，纯 Python 扫描行号更精确、两个平台输出一字不差。"""
    matches = []
    target = needle.lower() if ignore_case else needle
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            hit = line.lower() if ignore_case else line
            if target in hit:
                matches.append((p.name, lineno, line.rstrip("\r\n")))
    return matches

def _findstr_many(path: str, files: list[Path], needles: list[str],
                  ignore_case: bool = False) -> str:
    """一次扫描、多目标聚合。只服务"同一批 id 各出现多少行"这类任务。

    为什么需要它：T07 实测模型对 30 个 id 发了 31 次单 needle findstr，
    每次返回"总计 + 12 文件明细"约 320 字符，它一次没用过明细；
    39 次调用 / 13.9k 字符返回 / 75,105 tokens，其中 99% 的信息是噪声。
    """
    lines: list[str] = []
    for p in files:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            lines.extend(f.readlines())
    if not lines:
        return f"{path} 下的文件都是空的，没有可统计的行"

    counts = {}
    for n in needles:                       # 600 行 × 30 目标 = 18k 次 in，可忽略
        target = n.lower() if ignore_case else n
        if ignore_case:
            counts[n] = sum(1 for line in lines if target in line.lower())
        else:
            counts[n] = sum(1 for line in lines if target in line)

    zero = [n for n, c in counts.items() if c == 0]
    case_note = "（不区分大小写）" if ignore_case else "（区分大小写）"
    body = "\n".join(f"  {n}: {c}" for n, c in counts.items())
    head = (f"{path} 下 {len(files)} 个文件共 {len(lines)} 行，"
            f"{len(needles)} 个目标各自命中的行数{case_note}：")
    tail = ""
    if zero:
        # 只给汇总，不展开每文件明细；但保留"扫描了多少"这个自证
        tail = (f"\n零命中的 {len(zero)} 个（已扫描 {len(files)} 个文件、{len(lines)} 行，"
                f"不是没找到文件）：{'、'.join(zero)}")
    return head + "\n" + body + tail


def _findstr_lines(p_root: Path, needle: str, ignore_case: bool = False) -> list[tuple[str, int, str]]:
    """返回所有匹配行；优先使用带行号的系统命令，失败时回退到 Python。"""
    import subprocess

    try:
        if sys.platform == "win32":
            command = ["findstr", "/S", "/N", "/C:" + needle, "*.*"]
            if ignore_case:
                command.insert(2, "/I")
        else:
            command = ["grep", "-rFn", needle, "--include=*.*", "."]
            if ignore_case:
                command.insert(1, "-i")
        proc = subprocess.run(
            command, cwd=str(p_root), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        if proc.returncode == 1:  # 无匹配不是错误
            return []
        if proc.returncode == 0:
            matches = []
            for line in proc.stdout.splitlines():
                parts = line.split(":", 2)
                if len(parts) != 3 or not parts[1].isdigit():
                    continue
                name = parts[0].replace("\\", "/")
                if name.startswith("./"):
                    name = name[2:]
                matches.append((name, int(parts[1]), parts[2]))
            return matches
    except (FileNotFoundError, UnicodeDecodeError, OSError):
        pass

    matches = []
    target = needle.lower() if ignore_case else needle
    for p in p_root.rglob("*.*"):
        if not p.is_file():
            continue
        name = p.relative_to(p_root).as_posix()
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, 1):
                hit = line.lower() if ignore_case else line
                if target in hit:
                    matches.append((name, lineno, line.rstrip("\r\n")))
    return matches


def _findstr_subprocess(p_root: Path, needle: str, ignore_case: bool = False) -> dict | None:
    """尝试用系统命令一次性搜完；失败（找不到命令、输出异常）返回 None。"""
    import subprocess
    file_pattern = "*.*"
    try:
        if sys.platform == "win32":
            # Windows findstr: /S 递归 /C: 字面量（默认大小写敏感） /I 忽略大小写
            command = ["findstr", "/S", "/C:" + needle, file_pattern]
            if ignore_case:
                command.insert(2, "/I")
            proc = subprocess.run(
                command,
                cwd=str(p_root), capture_output=True, text=True, encoding="utf-8",
                errors="replace",
            )
            # exit code 1 = 无匹配（不是错误），>1 = 命令异常
            if proc.returncode > 1:
                return None
            return _parse_findstr_win(proc.stdout)
        else:
            # Unix-like grep: -r 递归 -F 字面量（默认大小写敏感） -i 忽略大小写
            command = ["grep", "-rF", needle, "--include=" + file_pattern, "."]
            if ignore_case:
                command.insert(1, "-i")
            proc = subprocess.run(
                command,
                cwd=str(p_root), capture_output=True, text=True, encoding="utf-8",
                errors="replace",
            )
            if proc.returncode > 1:
                return None
            return _parse_findstr_unix(proc.stdout)
    except (FileNotFoundError, UnicodeDecodeError, OSError):
        return None


def _parse_findstr_win(stdout: str) -> dict:
    # findstr 多文件输出:  filename:matched line
    per_file = {}
    for line in stdout.splitlines():
        if ":" not in line:
            continue
        # 行内容里可能本身有 : ，用 find 找第一个
        idx = line.find(":")
        fname = line[:idx]
        per_file[fname] = per_file.get(fname, 0) + 1
    return per_file


def _parse_findstr_unix(stdout: str) -> dict:
    # grep -rF 输出:  ./path/to/file:matched line
    per_file = {}
    for line in stdout.splitlines():
        if ":" not in line:
            continue
        idx = line.find(":")
        fname = line[:idx].lstrip("./")
        per_file[fname] = per_file.get(fname, 0) + 1
    return per_file


def _findstr_python(p_root: Path, needle: str, ignore_case: bool = False) -> dict:
    """回退方案：纯 Python 逐文件逐行读。键用相对路径，与 all_files 对齐，子目录同名文件不会互相覆盖。"""
    per_file = {}
    target = needle.lower() if ignore_case else needle
    for p in p_root.rglob("*.*"):
        if not p.is_file():
            continue
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            name = p.relative_to(p_root).as_posix()
            if ignore_case:
                per_file[name] = sum(1 for line in f if target in line.lower())
            else:
                per_file[name] = sum(1 for line in f if target in line)
    return per_file or None


# ---------- 新增能力 5：run_python（计算型任务，T08 分叉裁决） ----------
_runpy_seq = 0            # 执行过的代码在 artifacts/ 留档，便于审计与复现
RUN_PY_TIMEOUT = 5        # 默认超时秒数
RUN_PY_OUTPUT_MAX = 1000  # 输出截断上限；< 1200 落盘阈值，避免触发 offload 双跳

def run_python(code: str, timeout: int = RUN_PY_TIMEOUT) -> str:
    """执行一段 Python 代码，返回 stdout+stderr 合并文本；任何失败都回灌文本，不抛异常。

    为什么需要它（T08 实测 run 1119-194822）：没有计算工具时模型会在 reasoning 里
    逐个数数（200 值直方图 → 每步 +25k tokens、单条 111k、墙钟 6.5 分钟）。
    一次代码执行把"脑内模拟计算"换成真实计算。目标不是补能力——它没失败——
    是把每步 25k 的思考成本换成毫秒级执行。

    诚实边界：这不是安全沙箱。只限制了 cwd=SANDBOX 和 timeout；代码能读沙箱外的
    文件、能联网。威胁模型是"模型算错"，不是"模型作恶"——不要在 description
    或文档里写"安全"。
    """
    global _runpy_seq
    if not str(code).strip():
        return "run_python 需要 code 参数（非空 Python 代码）。四则运算请用 calc，更省"
    import sys as _sys, subprocess
    _runpy_seq += 1
    script = None
    try:                                   # RUN_DIR 未初始化时（单测）降级为 -c 执行
        (RUN_DIR / "artifacts").mkdir(parents=True, exist_ok=True)
        script = RUN_DIR / "artifacts" / f"runpy_{_runpy_seq}.py"
        script.write_text(str(code), encoding="utf-8")
    except Exception:
        script = None
    try:
        timeout = max(1, min(int(timeout or RUN_PY_TIMEOUT), 30))
        if script:
            # resolve() 必须：cwd=SANDBOX 时相对路径会被子进程解析到 sandbox 下
            proc = subprocess.run([_sys.executable, str(script.resolve())], cwd=SANDBOX,
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout)
        else:
            proc = subprocess.run([_sys.executable, "-c", str(code)], cwd=SANDBOX,
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout)
        out, err = (proc.stdout or "").strip(), (proc.stderr or "").strip()
        parts = [f"[exit {proc.returncode}]"]
        if out:
            parts.append(out)
        if err:
            parts.append("stderr:\n" + err)
        text = "\n".join(parts)
        if len(text) > RUN_PY_OUTPUT_MAX:
            text = (text[:RUN_PY_OUTPUT_MAX]
                    + f"\n[输出超过 {RUN_PY_OUTPUT_MAX} 字符已截断。"
                      "只 print 最终需要的数字；完整结果可写入 sandbox/ 下的文件再分段读]")
        return text
    except subprocess.TimeoutExpired:
        return (f"[超时] 代码执行超过 {timeout} 秒被终止。"
                "检查是否有死循环或全量遍历大文件，先采样再统计")
    except Exception as e:
        return f"[执行失败] {type(e).__name__}: {e}"


# 新增工具
EXTRA = {
    "findstr": findstr,
    "read_artifact": read_artifact,
    "write_report": write_report,
    "run_python": run_python,
}
# 新增工具描述
TOOLS = BASE_TOOLS + [
    {
        "type": "function",
        "function": {
            "name": "findstr",
            "description": "统计目录下所有文件中，包含指定字符串的行数；也可返回匹配行的文件名、行号和内容。"
               "path 传目录时一次性返回每个文件的计数。"
               "需要统计多个字符串（如 30 个 request_id 各出现多少行）时，必须用 needles 传数组一次搞定，"
               "不要一个字符串发一次调用——逐个调用会把每个目标的逐文件明细全塞进上下文（实测 31 次调用烧掉 75k tokens）。"
               "需要同时统计大小写变体（如 ERROR 和 error 的总行数）时传 ignore_case=true，一次调用即可，不要分别搜两遍再相加",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径。可以是目录（如 batch，递归搜索其下所有文件），也可以是单个文件（如 batch/log_16.log，只看这一个文件）"},
                    "needle": {"type": "string",
                    "description": "单个要查找的字符串，如 ERROR。用 needles 批量时省略"},
                    "needles": {"type": "array", "items": {"type": "string"},
                    "description": "批量模式：一次统计多个字符串，返回每个各命中多少行，最多 40 个。"
                                   "适用于“一组 id 各出现多少次”这类任务"},
                    "show_lines": {"type": "boolean", "description": "是否返回匹配行明细，默认 false"},
                    "max_lines": {"type": "integer", "description": "show_lines=true 时最多返回多少行，默认 12，超过 12 会被收紧到 12（防止输出过大被落盘）"},
                    "ignore_case": {"type": "boolean", "description": "不区分大小写匹配，默认 false。统计大小写变体总行数时必传 true，避免逐文件手工核对"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_artifact",
            "description": "读取之前被落盘的大结果文件。工具返回里出现 [结果过大已落盘] 时用它取细节。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "落盘名，如 tool_3"},
                    "offset": {"type": "integer", "description": "起始行，默认 0"},
                    "limit": {"type": "integer", "description": "读取行数，默认 200"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_report",
            "description": "把最终报告写入文件（markdown 格式）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径，如 report.md"},
                    "content": {"type": "string", "description": "完整文件内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "执行一段 Python 代码并返回 stdout/stderr。p50/p95/均值/占比/分组聚合等"
                "统计计算必须用本工具一次完成，严禁在回复中逐行数数或心算——实测对 200 行数据"
                "脑内做直方图会每步消耗 25k tokens。代码在 sandbox/ 目录下运行，可直接用 csv 等"
                "标准库读沙箱内文件（如 metrics/metrics.csv）。"
                "只 print 最终需要的数字，不要 print 中间数据，输出超过 1000 字符会被截断；"
                "默认 5 秒超时，超时会返回提示",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string",
                             "description": "完整可执行的 Python 代码（不是表达式——纯四则运算用 calc 更省）"},
                    "timeout": {"type": "integer",
                                "description": "超时秒数，默认 5，最大 30。遍历大文件时适当调大"},
                },
                "required": ["code"],
            },
        },
    },
]

# 新增工具调用
def dispatch2(name: str, args: dict) -> str:
    if name in EXTRA:
        return EXTRA[name](**args)
    return dispatch(name, args)


# ---------- 新增能力 1：留痕 ----------
def log(**ev) -> None:
    ev["ts"] = round(time.time(), 3)
    with open(RUN_DIR / "trace.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


# ---------- 新增能力 2：大结果外置 ----------
def offload(name: str, text: str) -> str:
    p = RUN_DIR / "artifacts" / f"{name}.txt"
    p.write_text(text, encoding="utf-8")
    preview = text[:240].replace("\n", " ⏎ ")
    return (
        f"[结果过大已落盘 name={name} 共 {len(text)} 字符] 开头摘要: {preview} ...\n"
        f"需要全文请调用 read_artifact(name='{name}', offset=0)。"
        f"若只需统计，请直接在摘要基础上判断，不要为了确认反复读取。"
    )


# ---------- 新增能力 3：checkpoint ----------
def save_state(state: dict) -> None:
    tmp = RUN_DIR / "state.json.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, RUN_DIR / "state.json")     # 原子替换，避免半截 JSON

# ---------- 上下文压缩（已关闭，负结果，详见 BASELINE.md「P2a」） ----------
# 结论：机制成立（窗口/落盘/幂等/逃生通道全部验证通过），成本不成立——
# 模型把被压缩的原文用 read_artifact 读回（t07 r1 读回 3 条 → 69k tokens 史上最贵），
# 压缩把"一次付费"变成"先存后取、取时再付"还加步数。原则：答案载体不可压缩。
# 保留注释备查；若未来重启，只压"过程性大返回"，保留数字行。
# COMPRESS_KEEP = 3          # 最近 K 条 tool 消息保留原文，按"条"数不按步数
# _compress_seq = 0          # 压缩落盘计数器：global 声明的前提是模块级先定义，否则 += 抛 NameError
#
# def compress_old_tools(messages: list, run_dir) -> int:
#     global _compress_seq
#     """把最近 K 条之外的 tool 消息原文落盘，content 换成摘要。返回压缩条数。"""
#     tool_idx = [i for i, m in enumerate(messages) if m["role"] == "tool"]
#     if len(tool_idx) <= COMPRESS_KEEP:
#         return 0
#     old = tool_idx[:-COMPRESS_KEEP]           # 只压老消息，最近 K 条永远不动
#     n = 0
#     for i in old:
#         m = messages[i]
#         if m["content"].startswith("[已压缩]"):
#             continue                          # 幂等：已压过的跳过
#         # 1) 原文先落盘（压缩必须可逆）
#         _compress_seq += 1
#         name = f"toolmsg_{_compress_seq}"
#
#         # 压缩前先落盘，避免被覆盖
#         offload(name, m["content"])
#         # 2) 摘要里的数字从原文第一行抽，不要自己算
#         first_line = m["content"].splitlines()[0][:80]
#         summary = (f"[已压缩] 原文 {len(m['content'])} 字符存于 artifacts/{name}.txt，"
#                    f"read_artifact('{name}', offset=0, limit=200) 取全文。首行：{first_line}")
#         m["content"] = summary
#         n += 1
#     return n

def ctx_chars(messages: list) -> int:
    return sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)


# ---------- 主循环 ----------
def run(question: str, client, self_test: bool = False, resume: str | None = None) -> tuple[Path, str]:
    global RUN_DIR
    # 模式只决定"根目录"（自检隔离到 _selftest/），分支决定"标识"（时间戳目录）
    base = RUNS / "_selftest" if self_test else RUNS
    if resume:
        # self_test 时 resume 名按 _selftest/ 下的子目录解析，与自检打印的名字一致
        RUN_DIR = base / resume if self_test else RUNS / resume
        state = json.loads((RUN_DIR / "state.json").read_text(encoding="utf-8"))
        print(f"[resume] 从第 {state['step']} 步继续，已有 {len(state['messages'])} 条消息，"
              f"上下文 {ctx_chars(state['messages'])} 字符")
        state.setdefault("seen", {})
        state.setdefault("no_progress", 0)
        log(event="resume", step=state["step"], model=MODEL, base_url=BASE_URL)
    else:
        RUN_DIR = base / (time.strftime("%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4])
        (RUN_DIR / "artifacts").mkdir(parents=True)
        state = {
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": question},
            ],
            "step": 0, "calls": {}, "tokens": 0, "seen": {}, "no_progress": 0,
        }
        log(event="start", question=question, model=MODEL, base_url=BASE_URL)
        print(f"[run] {RUN_DIR.name}")

    messages = state["messages"]
    tool_seq = len([1 for m in messages if m.get("role") == "tool"])

    while state["step"] < MAX_STEPS:
        state["step"] += 1
        t0 = time.time()
        resp = client.chat.completions.create(
            model=MODEL, messages=messages, tools=TOOLS, temperature=0
        )
        msg = resp.choices[0].message
        usage = resp.usage
        state["tokens"] += usage.total_tokens if usage else 0
        d = msg.model_dump(exclude_none=True)
        rc = d.pop("reasoning_content", None)
        if rc:
        #log(event="reasoning", step=state["step"], chars=len(rc))
            (RUN_DIR / "artifacts" / f"reasoning_step{state['step']}.txt").write_text(rc, encoding="utf-8")
        messages.append(d)

        # compress_old_tools(messages, RUN_DIR)   # P2a 已关闭（负结果），见 BASELINE.md

        if not msg.tool_calls:
            print(f"[step {state['step']}] 完成，上下文 {ctx_chars(messages)} 字符，"
                  f"累计 {state['tokens']} tokens")
            log(event="finish", step=state["step"], chars=ctx_chars(messages), tokens=state["tokens"])
            save_state(state)
            return RUN_DIR,msg.content or ""
        # 本步新信息计数，必须在 while 内、for 外
        step_new = 0                       
        for call in msg.tool_calls:
            args = json.loads(call.function.arguments or "{}")
            # key 必须是字符串：state 要落成 JSON，tuple 会被 json.dumps 拒绝
            key = call.function.name + "|" + json.dumps(args, sort_keys=True, ensure_ascii=False)
            state["calls"][key] = state["calls"].get(key, 0) + 1

            if state["calls"][key] >= 4:
                result = "同参数已调用 4 次，结果不会变。请换参数或依据已有信息继续。"
                print(f"[step {state['step']}] 拦截重复调用 {call.function.name}")
            else:
                try:
                    result = dispatch2(call.function.name, args)
                    # read_artifact 是逃生通道，绝不能再被落盘，否则会形成
                    # "读 -> 落盘 -> 再读" 的死循环；它靠 offset/limit 自带分页
                    if call.function.name != "read_artifact" and len(str(result)) > ARTIFACT_THRESHOLD:
                        tool_seq += 1
                        result = offload(f"tool_{tool_seq}", str(result))
                    print(f"[step {state['step']}] {call.function.name}"
                          f"({str(args)[:60]}) -> {len(str(result))} 字符")
                except Exception as e:
                    result = f"工具执行失败 {type(e).__name__}: {e}"
                    print(f"[step {state['step']}] {call.function.name} 报错: {e}")
            fp = fingerprint(result)                   # ← 注意：指纹打在这里，即模型最终看到的内容
            if fp not in state["seen"]:
                state["seen"][fp] = state["step"]
                step_new += 1
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": str(result),
            })

        # 整步所有调用都没带来新信息才算"无进展"
        state["no_progress"] = state["no_progress"] + 1 if step_new == 0 else 0
        if state["no_progress"] >= 2:
            messages.append({"role": "user", "content":
                f"你已经连续 {state['no_progress']} 步没有获得任何新信息，当前做法在原地打转。"
                "先复述你已确认的事实，再决定下一步；如果信息已足够，请直接作答。"})
            print(f"[step {state['step']}] 注入无进展提示（连续 {state['no_progress']} 步）")
            state["no_progress"] = 0       # 只提示一次，别每步都灌
        # prompt/completion/reasoning 三列是成本归因的地基：tokens 总量看不出
        # 钱花在"重发历史"还是"脑内计算"（T08 每步 +25k reasoning 就是这么发现的）
        # cache_hit/miss 列：重发历史的真实单价是 miss 的约 1/10（DeepSeek 前缀缓存），
        # 2026-11-19 靠这列在实现前否决了 P2b 上下文压缩（压缩会重写前缀、打掉缓存命中）
        log(event="step", step=state["step"], ms=round((time.time() - t0) * 1000),
            chars=ctx_chars(messages), tokens=state["tokens"],
            prompt=usage.prompt_tokens if usage else None,
            completion=usage.completion_tokens if usage else None,
            reasoning=getattr(getattr(usage, "completion_tokens_details", None),
                              "reasoning_tokens", None),
            cache_hit=getattr(usage, "prompt_cache_hit_tokens", None),
            cache_miss=getattr(usage, "prompt_cache_miss_tokens", None))
        save_state(state)                    # 每步落盘，这是能断点续跑的全部秘密

    print("[warn] 达到步数上限")
    return RUN_DIR,"未在限定步数内完成，已中止（state.json 仍在，可 --resume）。"


if __name__ == "__main__":
    args = sys.argv[1:]
    resume_id = args[args.index("--resume") + 1] if "--resume" in args else None

    if "--self-test" in args:
        from agent import FakeClient, _Call, _Msg
        if resume_id:
            print("=== 自检：恢复模式 ===")
            print(run(QUESTION, FakeClient([_Msg(content="(自检) 已完成并写入报告")]), self_test=True, resume=resume_id))
        else:
            print("=== 自检：正常模式，跑到一半会模拟崩溃 ===")
            try:
                run(QUESTION, FakeClient([
                    _Msg(tool_calls=[_Call(1, "list_files", {"subdir": "batch"})]),
                    _Msg(tool_calls=[_Call(2, "list_files", {"subdir": "batch"})]),
                    _Msg(tool_calls=[_Call(3, "list_files", {"subdir": "batch"})]),
                ]), self_test=True)     # 脚本耗尽 -> 抛异常，模拟进程被杀
            except IndexError:
                print(f"\n[模拟崩溃] 进程在容器里被 OOM kill 了。")
                print(f"继续跑: python agent2.py --resume {{上面打印的 run 目录名}} --self-test")
    else:
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            sys.exit("请先设置 OPENAI_API_KEY（或改用 --self-test）")
        # 启动诊断：换模型时最容易骗人的就是"以为改了 .env，其实读的是别处的变量"
        from envfile import _DEFAULT as ENV_FILE, source_of
        print(f"[config] model    = {MODEL}        ← {source_of('MODEL')}")
        print(f"[config] base_url = {BASE_URL}")
        print(f"[config] api_key  = {key[:6]}…{key[-4:]}（{len(key)} 位）← {source_of('OPENAI_API_KEY')}")
        print(f"[config] .env     = {ENV_FILE}{'' if ENV_FILE.exists() else '  [不存在]'}")
        print(f"[config] runs     = {RUNS}")
        print(run(QUESTION, OpenAI(api_key=key, base_url=BASE_URL), self_test=False, resume=resume_id))
