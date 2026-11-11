"""集中加载项目根目录的 ``.env``，让密钥只存在于环境变量，不落代码/配置.

设计：
- ``load_env()`` 在进程启动时调用一次即可（``src.common.config`` 导入时会自动调用）。
- 仅当系统未显式设置同名环境变量时才用 ``.env`` 填充（``override=False``），
  即 shell 里 ``set`` / ``export`` 的变量优先级高于 ``.env``。
- 未安装 ``python-dotenv`` 时静默跳过，不影响其它逻辑。
"""

from __future__ import annotations

from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]  # src/common -> 仓库根


def load_env() -> None:
    """加载仓库根目录的 ``.env``（若存在）。"""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    dotenv_path = _PROJECT_ROOT / ".env"
    if dotenv_path.exists():
        load_dotenv(dotenv_path, override=False)
