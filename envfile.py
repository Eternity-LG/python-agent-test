"""零依赖 .env 加载器。

关键约束：必须在任何"导入即定值"的模块之前调用。
agent.py 里 MODEL / BASE_URL 是模块顶层的 os.getenv，导入那一刻就固定了，
在 main() 里再读 .env 已经晚了——常量早就等于默认值。

只处理三种情况：KEY=VALUE、# 注释、去引号。三条规则：
  - 空值不注入。MODEL= 这种空行会把 os.getenv 的默认值顶成空串，API 直接 400。
  - 占位符不注入。<your-key> 这类留在 .env.example 里的东西不算真值。
  - setdefault 而非覆盖。真实环境变量优先级更高，.env 只负责补缺。
"""
import os
from pathlib import Path

_DEFAULT = Path(__file__).resolve().parent / ".env"


def load_env(path: Path | str | None = None) -> int:
    """把 .env 注入 os.environ，返回实际注入的条数（便于自检）。"""
    p = Path(path) if path else _DEFAULT
    if not p.exists():
        return 0
    n = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip("'\"")
        if not v or v.startswith("<"):
            continue
        os.environ.setdefault(k, v)
        n += 1
    return n
