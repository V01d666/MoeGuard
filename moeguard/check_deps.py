"""萌卫依赖版本检查工具。

验证所有运行时依赖是否已正确安装，打印版本信息。
退出码 0 = 全部就绪，1 = 缺少依赖。
"""

from __future__ import annotations

import sys
from collections.abc import Sequence


def _is_supported_python(version: Sequence[int]) -> bool:
    """仅接受当前发布基线 Python 3.12.x。"""
    return version[:2] == (3, 12)

# 必须的运行时依赖（import_name, pip_name, version_attr）
_REQUIRED: list[tuple[str, str, str]] = [
    ("PySide6", "PySide6", "__version__"),
    ("cv2", "opencv-python", "__version__"),
    ("numpy", "numpy", "__version__"),
    ("cryptography", "cryptography", "__version__"),
    ("PIL", "pillow", "__version__"),
    ("tomli_w", "tomli_w", "NOT_A_REAL_ATTR"),  # no standard version attr; _get_version falls back
]

def _get_version(mod, attr: str) -> str:
    """安全获取模块版本号。"""
    try:
        return str(getattr(mod, attr))
    except AttributeError:
        # 部分包用 version() 函数或 __version_info__ 元组
        for candidate in ("__version_info__", "VERSION", "version"):
            try:
                val = getattr(mod, candidate)
                if callable(val):
                    return str(val())
                return str(val)
            except AttributeError:
                continue
        return "(installed)"


def check() -> list[str]:
    """检查所有依赖，返回缺失/导入失败的描述列表（空=全部就绪）。"""
    problems: list[str] = []
    if not _is_supported_python((sys.version_info.major, sys.version_info.minor)):
        version = f"{sys.version_info.major}.{sys.version_info.minor}"
        problems.append(f"Unsupported Python {version}; MoeGuard requires Python 3.12.x")
        print(f"  [FAIL] Python {version} (requires 3.12.x)")
    for import_name, pip_name, attr in _REQUIRED:
        try:
            mod = __import__(import_name)
            version = _get_version(mod, attr)
            print(f"  [OK] {pip_name:<24} {version}")
        except ImportError:
            problems.append(f"Missing {pip_name} (pip install {pip_name})")
            print(f"  [MISS] {pip_name}")
        except Exception as exc:
            problems.append(f"Import failed for {pip_name}: {exc}")
            print(f"  [FAIL] {pip_name}: {exc}")
    return problems


def main() -> int:
    """入口：打印依赖检查结果，返回退出码。"""
    print("MoeGuard Dependency Check")
    print(f"Python {sys.version}")
    print()
    problems = check()
    print()
    if problems:
        print("Problems found:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("All dependencies ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
