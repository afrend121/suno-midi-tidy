#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""suno-midi-tidy 图形界面（Windows 文件夹选择）。

tkinter 是标准库，但部分精简版 Python 不含它。这里做两路降级：
  有 tkinter  -> 图形界面
  没有 tkinter -> 纯文本交互，功能完全一致

界面完全由**能力集**驱动（`licensing.EditionInfo.caps`）：能力开放即控件可用，
未开放则灰显并标注。当前能力集为全集，因此所有选项都可用；若将来重新分层，
界面无需改动即可自动收敛。也可以直接用 midi_tidy.py（命令行）。
"""

from __future__ import annotations

import contextlib
import io
import os
import queue
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import licensing  # noqa: E402
from tidy_core import (TidyConfig, discover_pairs, emit_pair,  # noqa: E402
                       print_report, process_pair)

TIME_SIGS = ["（不写入）", "4/4", "3/4", "6/8", "2/4", "12/8"]
GRIDS = {"1/8 音符": 2, "1/16 音符": 4, "1/32 音符": 8}


def _can(info: licensing.EditionInfo, cap: str) -> bool:
    """该能力是否放行。界面所有开关都按这个判断，不按版本名判断。"""
    return cap in info.caps


def make_config(out: str, time_sig: str, grid: str, opts: dict,
                info: licensing.EditionInfo) -> TidyConfig:
    cfg = TidyConfig(
        out_dir=out,
        time_sig=None if time_sig.startswith("（") else time_sig,
        grid_div=GRIDS.get(grid, 4),
        apply_align=opts.get("align", True),
        align_mode="midi" if opts.get("align", True) else "none",
        delete_duplicates=opts.get("dedup", True),
        delete_silent=opts.get("desilence", True),
        vocal_check="on" if opts.get("vocal", True) else "off",
        emit_mix=opts.get("mix", True),
        caps=info.caps,
    )
    if opts.get("conservative"):
        cfg.snr_db_min = 8.0
        cfg.vocal_check = "off"
        cfg.dup_tol_ms = 25.0
    return cfg


def run_job(inp: str, out: str, time_sig: str, grid: str, opts: dict, log,
            info: licensing.EditionInfo | None = None) -> int:
    if info is None:
        info = licensing.resolve()
    cfg = make_config(out, time_sig, grid, opts, info)
    locked = licensing.apply_caps(cfg, info.caps, info)

    log(licensing.edition_banner(info))
    if locked:
        log("本次未启用（能力未开放）：%s" % "，".join(locked))

    pairs = discover_pairs(inp)
    if not pairs:
        log("没有找到同名的 .mid / .wav 配对。")
        log("请把同一条分轨的 MIDI 与 WAV 用相同文件名放在一起，例如：")
        log("    我的歌 (Bass).mid   我的歌 (Bass).wav")
        return 2

    if not _can(info, licensing.CAP_BATCH) and len(pairs) > licensing.FREE_MAX_FILES:
        log("本次只处理 %d 个配对，其余 %d 个已跳过。"
            % (licensing.FREE_MAX_FILES, len(pairs) - licensing.FREE_MAX_FILES))
        pairs = pairs[:licensing.FREE_MAX_FILES]

    log("发现 %d 组配对：" % len(pairs))
    for m, w in pairs:
        log("  %s  +  %s" % (os.path.basename(m), os.path.basename(w)))

    reports = []
    for m, w in pairs:
        try:
            r = process_pair(m, w, cfg, out, emit=False)
        except licensing.EditionLimit as exc:
            log("已到上限：%s" % exc)
            log("请裁短该分轨后重试。")
            return 3
        except Exception as exc:                                  # noqa: BLE001
            log("处理失败 %s: %s" % (os.path.basename(m), exc))
            continue
        reports.append(r)
        log("")
        sio = io.StringIO()
        with contextlib.redirect_stdout(sio):
            print_report(r)
        log(sio.getvalue())

    if opts.get("dry_run"):
        log("[只分析] 未写入任何文件。")
        return 0

    for r in reports:
        emit_pair(r, cfg)

    log("")
    log("完成。输出：")
    if cfg.emit_mix:
        log("  %s/mix/     演奏时间细节版（Cubase / Ableton）" % out)
    if cfg.emit_score:
        log("  %s/score/   量化版（MuseScore 出谱）" % out)
    log("  %s/report/  %s" % (out, "CSV + JSON 报告，含被删音符清单"
                              if cfg.full_report else "汇总报告"))
    log("原始文件未被修改。")
    return 0


# --------------------------------------------------------------------------
# 图形界面
# --------------------------------------------------------------------------

def run_gui() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    info = licensing.resolve()

    root = tk.Tk()
    root.title("Suno MIDI 整音 — MIDI Tidy（全功能免费版）")
    root.geometry("860x680")
    root.minsize(760, 560)

    logq: "queue.Queue[str]" = queue.Queue()
    running = {"v": False}

    def row(parent, r):
        f = tk.Frame(parent)
        f.grid(row=r, column=0, sticky="ew", pady=3)
        f.columnconfigure(1, weight=1)
        return f

    body = tk.Frame(root, padx=12, pady=10)
    body.pack(fill="both", expand=True)
    body.columnconfigure(0, weight=1)
    body.rowconfigure(8, weight=1)

    # 版本条
    bar = tk.Frame(body, bg="#e8f4ff")
    bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
    tk.Label(bar, text="全功能免费版 · " + info.message,
             bg=bar["bg"], fg="#12507b",
             anchor="w", padx=10, pady=5).pack(fill="x")

    in_var = tk.StringVar()
    out_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "MIDI_Tidy_out"))
    ts_var = tk.StringVar(value="（不写入）")
    grid_var = tk.StringVar(value="1/16 音符")
    # 默认值按能力集给：能力开放就默认开启
    opt = {k: tk.BooleanVar(value=v) for k, v in
           dict(align=True, dedup=True,
                desilence=_can(info, licensing.CAP_SILENT_PRUNE),
                vocal=_can(info, licensing.CAP_VOCAL_VERIFY),
                conservative=False, dry_run=False,
                mix=_can(info, licensing.CAP_MIX_OUTPUT)).items()}

    f = row(body, 1)
    tk.Label(f, text="分轨文件夹", width=12, anchor="w").grid(row=0, column=0)
    tk.Entry(f, textvariable=in_var).grid(row=0, column=1, sticky="ew", padx=4)
    tk.Button(f, text="浏览…", width=8,
              command=lambda: in_var.set(filedialog.askdirectory() or in_var.get())
              ).grid(row=0, column=2)

    f = row(body, 2)
    tk.Label(f, text="输出文件夹", width=12, anchor="w").grid(row=0, column=0)
    tk.Entry(f, textvariable=out_var).grid(row=0, column=1, sticky="ew", padx=4)
    tk.Button(f, text="浏览…", width=8,
              command=lambda: out_var.set(filedialog.askdirectory() or out_var.get())
              ).grid(row=0, column=2)

    f = row(body, 3)
    tk.Label(f, text="拍号", width=12, anchor="w").grid(row=0, column=0)
    ttk.Combobox(f, textvariable=ts_var, values=TIME_SIGS, width=12,
                 state=("readonly" if _can(info, licensing.CAP_TIMESIG) else "disabled")
                 ).grid(row=0, column=1, sticky="w", padx=4)
    tk.Label(f, text="出谱量化栅格").grid(row=1, column=0, sticky="w", pady=3)
    ttk.Combobox(f, textvariable=grid_var, values=list(GRIDS), state="readonly",
                 width=12).grid(row=1, column=1, sticky="w", padx=4)
    if not _can(info, licensing.CAP_TIMESIG):
        tk.Label(f, text="↑ 拍号写入未开放", fg="#b06a00").grid(
            row=0, column=2, sticky="w")

    f = tk.Frame(body)
    f.grid(row=4, column=0, sticky="ew", pady=6)
    labels = [("align", "应用起音对齐", _can(info, licensing.CAP_ALIGN_APPLY)),
              ("dedup", "清理重复音", _can(info, licensing.CAP_DUP_PRUNE)),
              ("desilence", "删除无声音符", _can(info, licensing.CAP_SILENT_PRUNE)),
              ("vocal", "人声音高核验", _can(info, licensing.CAP_VOCAL_VERIFY)),
              ("mix", "输出 mix 演奏版", _can(info, licensing.CAP_MIX_OUTPUT)),
              ("conservative", "保守模式", True),
              ("dry_run", "只分析不写文件", True)]
    for i, (k, t, enabled) in enumerate(labels):
        cb = tk.Checkbutton(f, text=t + ("" if enabled else "（未开放）"),
                            variable=opt[k],
                            state=("normal" if enabled else "disabled"))
        if not enabled:
            cb.configure(fg="#9a9a9a")
        cb.grid(row=i // 3, column=i % 3, sticky="w", padx=6)

    btns = tk.Frame(body)
    btns.grid(row=5, column=0, sticky="ew", pady=4)

    def start():
        if running["v"]:
            return
        inp = in_var.get().strip()
        if not inp or not os.path.isdir(inp):
            messagebox.showwarning("提示", "请先选择分轨文件夹。")
            return
        out = out_var.get().strip() or "tidy_out"
        try:
            if os.path.abspath(out).startswith(os.path.abspath(inp) + os.sep):
                messagebox.showwarning("提示", "输出文件夹不能位于分轨文件夹内部，以免覆盖原始文件。")
                return
        except Exception:
            pass
        running["v"] = True
        status.config(text="处理中…")

        def worker():
            o = {k: v.get() for k, v in opt.items()}
            try:
                run_job(inp, out, ts_var.get(), grid_var.get(), o, logq.put, info)
            except Exception as exc:                              # noqa: BLE001
                logq.put("运行出错：%s" % exc)
            finally:
                logq.put(None)

        threading.Thread(target=worker, daemon=True).start()

    tk.Button(btns, text="开始整理", width=16, height=2, command=start).pack(side="left", padx=4)
    tk.Button(btns, text="打开输出目录", width=14,
              command=lambda: os.startfile(out_var.get()) if os.path.isdir(out_var.get())
              else messagebox.showinfo("提示", "输出目录还不存在。")).pack(side="left", padx=4)
    tk.Button(btns, text="关于", width=14,
              command=lambda: messagebox.showinfo(
                  "关于 suno-midi-tidy",
                  "版本：全功能免费版\n\n%s\n\n"
                  "全部能力免费开放，无需授权码，全程离线。\n"
                  "命令行用法：python scripts/midi_tidy.py --help"
                  % info.message)
              ).pack(side="left", padx=4)

    status = tk.Label(body, text="就绪", anchor="w", fg="#555")
    status.grid(row=6, column=0, sticky="ew")

    txt = tk.Text(body, height=18, wrap="word")
    txt.grid(row=8, column=0, sticky="nsew")
    sb = tk.Scrollbar(body, command=txt.yview)
    txt.configure(yscrollcommand=sb.set)

    def log(msg: str) -> None:
        logq.put(msg)

    def pump():
        while True:
            try:
                item = logq.get_nowait()
            except queue.Empty:
                break
            if item is None:
                running["v"] = False
                status.config(text="完成（可回听被删音符清单后再决定是否接受）")
                continue
            txt.insert("end", str(item).rstrip() + "\n")
            txt.see("end")
            if not running["v"]:
                status.config(text="就绪")
        root.after(120, pump)

    txt.insert("end", "把同一条 Suno 分轨的 .mid 与 .wav 用相同文件名放在一个文件夹里，\n"
                      "选择该文件夹后点「开始整理」。原始文件不会被修改。\n\n")
    pump()
    root.mainloop()
    return 0


# --------------------------------------------------------------------------
# 无 tkinter 时的文本交互
# --------------------------------------------------------------------------

def run_cli_fallback() -> int:
    info = licensing.resolve()
    print(licensing.edition_banner(info))
    print("未检测到 tkinter（当前 Python 不含图形界面模块）。")
    print("改用文本交互，功能完全一致。\n")
    inp = input("分轨文件夹路径: ").strip().strip('"')
    if not os.path.isdir(inp):
        print("路径不存在。")
        return 2
    out = input("输出文件夹 [默认 tidyrun_out]: ").strip().strip('"') or "tidyrun_out"
    ts = input("拍号（回车跳过，如 3/4）: ").strip() or None
    dry = input("只分析不写文件？(y/N): ").strip().lower() == "y"
    opts = dict(align=True, dedup=True, desilence=True, vocal=True,
                conservative=False, dry_run=dry, mix=True)
    return run_job(inp, out, ts or "（不写入）", "1/16 音符", opts, print, info)


def main() -> int:
    try:
        import tkinter  # noqa: F401
    except Exception:
        return run_cli_fallback()
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
