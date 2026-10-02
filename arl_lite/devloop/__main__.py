"""arl_lite.devloop.__main__ — 支持 python3 -m arl_lite.devloop"""
from __future__ import annotations

import sys

from .cli import cmd_devloop


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="python3 -m arl_lite.devloop",
        description="arl-lite 自持迭代协议",
    )
    sub = p.add_subparsers(dest="devloop_sub", required=True)
    sub.add_parser("status", help="当前状态")
    pt = sub.add_parser("test", help="只跑门禁")
    pt.add_argument("--gates", default=None)
    pr = sub.add_parser("round", help="跑一整轮")
    pr.add_argument("--gates", default=None)
    sub.add_parser("plan", help="只做规划")
    ph = sub.add_parser("history", help="最近几轮")
    ph.add_argument("-n", type=int, default=10)
    pg = sub.add_parser("gate", help="跑单个门禁")
    pg.add_argument("gate_name")

    args = p.parse_args(argv)
    return cmd_devloop(args)


if __name__ == "__main__":
    sys.exit(main())
