#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""suno-midi-tidy 命令行入口（全功能免费版）

典型用法：
    # 只报告偏移量，不写文件（先看再决定）
    python midi_tidy.py --input 分轨文件夹 --out tidy_out --dry-run

    # 完整处理，3/4 拍，量化到十六分音符
    python midi_tidy.py --input 分轨文件夹 --out tidy_out --time-sig 3/4 --yes

    # 保守模式：只处理有把握的，宁可不删
    python midi_tidy.py --input 分轨文件夹 --out tidy_out --conservative --yes

诊断：
    python midi_tidy.py --show-license            # 查看当前能力开放情况
    python midi_tidy.py --machine-id              # 打印本机机器码
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import licensing  # noqa: E402
from tidy_core import (TidyConfig, discover_pairs, emit_pair,  # noqa: E402
                       print_report, process_pair)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="midi_tidy",
        description="Suno MIDI 整音 —— 用 WAV 修正 MIDI 的力度、误删与起音对齐")
    p.add_argument("--input", "-i", required=False,
                   help="包含同名 .mid/.wav 的文件夹（或单个 .mid 文件）")
    p.add_argument("--out", "-o", default="tidy_out", help="输出目录（默认 tidy_out）")
    p.add_argument("--time-sig", default=None, help="拍号，如 3/4、4/4（写入 MIDI 元事件）")
    p.add_argument("--grid", type=int, default=4,
                   help="score 版量化栅格 = 1/N 分音符（默认 4=十六分音符）")
    p.add_argument("--max-offset-ms", type=float, default=450.0,
                   help="起音偏移搜索上限（默认 450ms）")
    p.add_argument("--context-sec", type=float, default=4.0,
                   help="局部响度上下文半径（默认 4.0 秒，对应原文“前后约四秒”）")
    p.add_argument("--snr-db-min", type=float, default=4.0,
                   help="判定“WAV 里没声音”的局部信噪比阈值，越高越保守（默认 4dB）")
    p.add_argument("--note-win-ms", type=float, default=90.0,
                   help="力度取样 / 有声判据窗口（默认 90ms）")
    p.add_argument("--dup-tol-ms", type=float, default=35.0,
                   help="判定重复音的起点间隔容差（默认 35ms）")
    p.add_argument("--no-align", action="store_true", help="只报告偏移，不改 MIDI 时间轴")
    p.add_argument("--no-dedup", action="store_true", help="跳过重复音清理")
    p.add_argument("--no-delete-silent", action="store_true", help="跳过无声音符清理")
    p.add_argument("--vocal-check", choices=["auto", "on", "off"], default="auto",
                   help="人声轨音高证据核验（默认 auto：按轨名自动识别）")
    p.add_argument("--vocal-min-evidence-db", type=float, default=6.0,
                   help="人声保留所需的音高证据下限（默认 6dB）")
    p.add_argument("--conservative", action="store_true",
                   help="保守预设：提高删除门槛、跳过人声清理")
    p.add_argument("--dry-run", action="store_true",
                   help="只分析并报告，不写任何输出文件")
    p.add_argument("--yes", "-y", action="store_true", help="跳过交互确认（非交互环境必需）")
    p.add_argument("--no-score", action="store_true", help="不输出 score 目录（量化版）")
    p.add_argument("--no-mix", action="store_true", help="不输出 mix 目录（演奏版）")
    # ---- 版本 / 授权 ----
    p.add_argument("--license", default=None,
                   help="授权码文件路径（历史兼容，当前不影响功能放行）")
    p.add_argument("--edition", choices=["free", "pro"], default=None,
                   help="强制以指定版本运行（诊断用）")
    p.add_argument("--machine-id", action="store_true",
                   help="打印本机机器码后退出")
    p.add_argument("--show-license", action="store_true",
                   help="打印当前能力开放情况后退出")
    return p


def confirm(question: str, auto_yes: bool) -> bool:
    if auto_yes:
        return True
    if not sys.stdin.isatty():
        print("（非交互环境，默认不执行。加 --yes 可确认。）")
        return False
    try:
        return input(question + " [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.machine_id:
        print(licensing.machine_id())
        return 0

    info = licensing.resolve(args.license, args.edition)
    print(licensing.edition_banner(info))

    if args.show_license:
        print("版本    : %s" % ("专业版 PRO" if info.is_pro else "全功能免费版"))
        print("授权状态: %s" % info.status)
        print("说明    : %s" % info.message)
        print("能力    : %d / %d 项已开放%s"
              % (len(info.caps), len(licensing.ALL_CAPS),
                 "" if len(info.caps) == len(licensing.ALL_CAPS)
                 else "（未开放：%s）" % "，".join(info.locked)))
        print("机器码  : %s" % licensing.machine_id())
        print("授权来源: %s" % (info.source or "-"))
        return 0

    if not args.input:
        print("错误：缺少 --input（或使用 --show-license / --machine-id）")
        return 2

    target_edition = "专业版 PRO" if info.is_pro else "全功能免费版"
    cfg = TidyConfig(
        out_dir=args.out, max_offset_ms=args.max_offset_ms,
        apply_align=not args.no_align, align_mode="midi" if not args.no_align else "none",
        dup_tol_ms=args.dup_tol_ms, delete_duplicates=not args.no_dedup,
        delete_silent=not args.no_delete_silent, snr_db_min=args.snr_db_min,
        note_win_ms=args.note_win_ms, context_sec=args.context_sec,
        vocal_check=args.vocal_check, vocal_min_evidence_db=args.vocal_min_evidence_db,
        grid_div=args.grid, time_sig=args.time_sig,
        emit_mix=not args.no_mix, emit_score=not args.no_score,
        caps=info.caps)
    if args.conservative:
        cfg.snr_db_min = 8.0
        cfg.vocal_check = "off"
        cfg.dup_tol_ms = 25.0

    # 按能力集钳制配置（当前能力集为全集，钳制结果为空）
    locked = licensing.apply_caps(cfg, info.caps, info)
    if locked:
        print("本次未启用（能力未开放）：%s" % "，".join(locked))
        print()

    # 收集配对
    if os.path.isfile(args.input):
        folder = os.path.dirname(os.path.abspath(args.input)) or "."
        pairs = [p for p in discover_pairs(folder)
                 if os.path.abspath(p[0]) == os.path.abspath(args.input)]
        if not pairs:
            print("找不到与 %s 同名的 WAV" % args.input)
            return 2
    elif os.path.isdir(args.input):
        pairs = discover_pairs(args.input)
    else:
        print("输入路径不存在: %s" % args.input)
        return 2

    if not pairs:
        print("在 %s 里没有找到同名的 .mid/.wav 配对。" % args.input)
        print("请确认：同一分轨的 MIDI 与 WAV 使用相同文件名，例如 "
              "'我的歌 (Bass).mid' 与 '我的歌 (Bass).wav'。")
        return 2

    if licensing.CAP_BATCH not in info.caps and len(pairs) > licensing.FREE_MAX_FILES:
        print("本次只处理 %d 个配对，其余 %d 个已跳过。"
              % (licensing.FREE_MAX_FILES, len(pairs) - licensing.FREE_MAX_FILES))
        pairs = pairs[:licensing.FREE_MAX_FILES]

    print("发现 %d 组配对（%s）：" % (len(pairs), target_edition))
    for m, w in pairs:
        print("  - %s  +  %s" % (os.path.basename(m), os.path.basename(w)))

    # 第一阶段：只分析并报告偏移，不写任何文件
    reports = []
    for m, w in pairs:
        try:
            r = process_pair(m, w, cfg, args.out, log=print, emit=False)
        except licensing.EditionLimit as exc:
            print("已到上限：%s" % exc)
            print("请裁短该分轨后重试。")
            return 3
        except Exception as exc:  # noqa: BLE001
            print("处理失败 %s: %s" % (os.path.basename(m), exc))
            continue
        reports.append(r)
        print_report(r)

    if not reports:
        return 1

    if args.dry_run:
        print("\n[dry-run] 未写入任何文件。确认无误后去掉 --dry-run 重新运行。")
        return 0

    offs = [r.offset_ms for r in reports if abs(r.offset_ms) >= 1.0]
    if offs and cfg.apply_align and not args.yes:
        mean_off = sum(offs) / len(offs)
        print("\n检测到起音偏移（平均 %+.0f ms）。对齐后力度会按新时间轴重算。" % mean_off)
        if not confirm("是否应用对齐并写出文件？", False):
            print("已取消。原始文件未被修改。")
            return 0

    for r in reports:
        emit_pair(r, cfg)

    print("\n输出目录结构：")
    if cfg.emit_mix:
        print("  %s/mix/     演奏时间细节版（Cubase / Ableton）" % args.out)
    if cfg.emit_score:
        print("  %s/score/   量化版（MuseScore 出谱）" % args.out)
    print("  %s/report/  %s" % (args.out, "CSV + JSON 报告，含被删音符清单"
                                if cfg.full_report else "汇总报告"))
    print("原始文件未被修改。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
