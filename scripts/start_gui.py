#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Suno MIDI 整音 · 图形界面启动器（跨平台）

    python scripts/start_gui.py            启动图形界面
    python scripts/start_gui.py --check    只做环境自检，不启动

为什么用 .py 而不是 .bat：
    `.bat` / `.cmd` 属于可执行脚本，技能市场（如 SkillHub）
    不接受技能包内含有此类文件。本脚本用 Python 实现同样的能力，
    任何平台都能跑，也不再被审核拦截。

它只做两件事：

  1. **环境自检** —— 缺哪个依赖、该敲哪条命令，直接说清楚，
     而不是甩一个 ImportError 出来；
  2. **转交** `scripts/gui.py` —— 没有 tkinter 时，gui.py 会自动
     降级为文本交互，功能完全一致。

安全说明：本脚本不创建进程、不联网、不写任何系统目录，
只做 import 探测与同进程内的一次函数调用。
"""

from __future__ import annotations

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
REQ_FILE = os.path.join(ROOT, "requirements.txt")

# (模块名, 是否必需, 说明)
DEPS = (
    ("numpy", True, "数值计算"),
    ("scipy", True, "信号处理"),
    ("soundfile", True, "读取 WAV"),
    ("pretty_midi", True, "读写 MIDI"),
    ("tkinter", False, "图形界面；缺失时自动降级为文本交互"),
)


def probe() -> "list[tuple[str, bool, str]]":
    """返回 (模块名, 是否必需, 说明) 中**缺失**的那些。"""
    missing = []
    for mod, required, desc in DEPS:
        try:
            found = importlib.util.find_spec(mod) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append((mod, required, desc))
    return missing


def _quote(p: str) -> str:
    return '"%s"' % p if " " in p else p


def install_hint() -> str:
    target = _quote(REQ_FILE) if os.path.isfile(REQ_FILE) else \
        "numpy scipy soundfile pretty_midi"
    return "%s -m pip install -r %s -i %s" % (
        _quote(sys.executable), target, MIRROR)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    check_only = "--check" in argv or "-c" in argv

    print("=" * 62)
    print("  Suno MIDI 整音  ·  图形界面启动器")
    print("=" * 62)
    print("  Python   : %s" % sys.version.split()[0])
    print("  解释器   : %s" % sys.executable)
    print("  技能目录 : %s" % ROOT)

    missing = probe()
    hard = [m for m, required, _d in missing if required]
    no_tk = any(m == "tkinter" for m, _r, _d in missing)

    print("  依赖状态 : %s" % ("全部就绪" if not missing else
                             "缺少 %d 项" % len(missing)))
    if no_tk:
        print("  界面模式 : 文本交互（当前 Python 不含 tkinter）")
    else:
        print("  界面模式 : 图形界面")
    print("=" * 62)

    if hard:
        print("\n缺少必需依赖：")
        for mod, _r, desc in missing:
            if mod == "tkinter":
                continue
            print("    - %-12s %s" % (mod, desc))
        print("\n复制这一行直接装（国内走清华镜像）：\n")
        print("    %s\n" % install_hint())
        print("装好后重新运行：")
        print("    %s %s\n" % (_quote(sys.executable),
                              _quote(os.path.join(HERE, "start_gui.py"))))
        if no_tk:
            print("提示：当前解释器没有 tkinter，即使装好依赖也只会进文本交互。")
            print("      想要图形界面，请装 python.org 的官方安装包（默认含 tkinter）。\n")
        return 2

    if check_only:
        print("\n环境自检通过，可以直接启动界面：")
        print("    %s %s\n" % (_quote(sys.executable),
                              _quote(os.path.join(HERE, "start_gui.py"))))
        if no_tk:
            print("（当前解释器无 tkinter，将进入文本交互模式。）\n")
        return 0

    print("\n启动界面...\n")
    sys.path.insert(0, HERE)
    try:
        import gui                                          # noqa: E402
    except Exception as exc:                                # noqa: BLE001
        print("[ERROR] 无法加载 scripts/gui.py：%s: %s" % (type(exc).__name__, exc))
        print("        也可以直接用命令行：")
        print("        %s %s --input 分轨文件夹 --out 整理输出 --dry-run"
              % (_quote(sys.executable),
                 _quote(os.path.join(HERE, "midi_tidy.py"))))
        return 1
    return gui.main()


if __name__ == "__main__":
    raise SystemExit(main())
