"""零依赖 .env 加载器。

关键约束：必须在任何"导入即定值"的模块之前调用。
agent.py 里 MODEL / BASE_URL 是模块顶层的 os.getenv，导入那一刻就固定了，
在 main() 里再读 .env 已经晚了——常量早就等于默认值。

优先级：**项目里的 .env 覆盖系统环境变量**（override=True，默认）。
理由：.env 是这个项目显式声明的配置；系统/IDE 里遗留的 OPENAI_API_KEY
多半是给别的项目用的，让它静默顶掉 .env 的表现就是"改了 .env 没反应"。
CI 里需要注入密钥赢过仓库文件时，传 override=False 即可。

另外两条规则：
  - 空值不注入。MODEL= 这种空行会把 os.getenv 的默认值顶成空串，API 直接 400。
  - 占位符不注入。<your-key> 这类模板残留不算真值。
"""
import os
from pathlib import Path

_DEFAULT = Path(__file__).resolve().parent / ".env"


def load_env(path: Path | str | None = None, override: bool = True) -> int:
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
        if override:
            os.environ[k] = v
        else:
            os.environ.setdefault(k, v)
        n += 1
    return n


def source_of(key: str) -> str:
    """给启动诊断用：这个值现在到底来自 .env，还是被外部环境顶掉了。"""
    env_path = _DEFAULT
    if not env_path.exists():
        return "环境变量（没有 .env 文件）"
    current = os.getenv(key)
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() != key:
            continue
        want = v.strip().strip("'\"")
        if want == current:
            return f".env（{env_path}）"
        return f"环境变量（.env 里是 {want!r}，没生效！）"
    return "环境变量（.env 里没有这一项）"
