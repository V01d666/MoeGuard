"""萌卫应用入口。"""

from __future__ import annotations

import sys

from moeguard.app import run


def main() -> int:
    """启动萌卫桌面应用，返回退出码。"""
    return run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
