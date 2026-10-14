#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成"答案已知"的合成测试素材。

原文最关键的一条方法论：**先造一组答案已知的测试音乐**。
程序生成的 MIDI 通常都能打开，实际听起来却可能是错的；先用故意插入
错音与时间偏差的文件过关，再拿真实歌曲试，才能分清哪些问题来自
原始转写、哪些是新代码弄坏的。

三组素材：
  bass        15 个 MIDI 音符 / 13 个真实发声 / 1 个重复音 / 1 个哑音，
              全部 Velocity=53，WAV 故意晚 180ms。
  quietintro  安静前奏(-42dB) + 主歌(-24dB)，检验局部窗口是否防误删。
  vocal       人声轨混入 2 个假音符（八度下方低音 + 无关音高）+ 伴奏串音。
"""

from __future__ import annotations

import os
import sys
import wave

import numpy as np
import pretty_midi

SR = 22050
DITHER = 3.0e-5          # 极低底噪，模拟真实录音噪声地板


# --------------------------------------------------------------------------
# 合成
# --------------------------------------------------------------------------

def _adsr(n: int, sr: int, attack: float = 0.006, tau: float = 0.12) -> np.ndarray:
    t = np.arange(n) / sr
    env = np.exp(-t / tau)
    a = max(1, int(attack * sr))
    env[:a] *= np.linspace(0.0, 1.0, a)
    return env


def render_tone(y: np.ndarray, sr: int, start: float, dur: float, freq: float,
                amp: float, n_harm: int = 6) -> None:
    """把一段带谐波的衰减音叠加到缓冲区（原地修改）。"""
    i0 = int(start * sr)
    n = int(dur * sr)
    if n <= 0 or i0 >= len(y):
        return
    n = min(n, len(y) - i0)
    t = np.arange(n) / sr
    sig = np.zeros(n)
    for h in range(1, n_harm + 1):
        f = freq * h
        if f > sr / 2:
            break
        sig += (1.0 / h) * np.sin(2 * np.pi * f * t + 0.3 * h)
    sig *= _adsr(n, sr)
    sig /= (np.max(np.abs(sig)) + 1e-9)
    y[i0:i0 + n] += (amp * sig).astype(np.float32)


def render_vowel(y: np.ndarray, sr: int, start: float, dur: float, freq: float,
                 amp: float) -> None:
    """歌手：基频最强 + 缓慢颤音，便于音高证据检测。"""
    i0 = int(start * sr)
    n = int(dur * sr)
    if n <= 0 or i0 >= len(y):
        return
    n = min(n, len(y) - i0)
    t = np.arange(n) / sr
    vib = 1.0 + 0.004 * np.sin(2 * np.pi * 5.2 * t)
    sig = 0.62 * np.sin(2 * np.pi * freq * np.cumsum(vib) / sr)
    sig += 0.24 * np.sin(2 * np.pi * 2 * freq * t)
    sig += 0.10 * np.sin(2 * np.pi * 3 * freq * t)
    sig += 0.04 * np.sin(2 * np.pi * 4 * freq * t)
    sig *= _adsr(n, sr, attack=0.03, tau=0.30)
    sig /= (np.max(np.abs(sig)) + 1e-9)
    y[i0:i0 + n] += (amp * sig).astype(np.float32)


def add_noise_floor(y: np.ndarray, level_dbfs: float = -55.0, seed: int = 7) -> None:
    rng = np.random.default_rng(seed)
    n = rng.normal(0.0, 1.0, len(y))
    # 一阶低通，接近粉噪，避免高频过于干净
    n = np.convolve(n, np.ones(6) / 6.0, mode="same")
    n /= (np.max(np.abs(n)) + 1e-9)
    y += (n * (10 ** (level_dbfs / 20.0))).astype(np.float32)


def save_wav(path: str, y: np.ndarray, sr: int = SR) -> None:
    rng = np.random.default_rng(11)
    y = y + rng.normal(0.0, DITHER, len(y)).astype(np.float32)
    peak = float(np.max(np.abs(y))) or 1.0
    if peak > 0.99:
        y = y * (0.99 / peak)
    pcm = np.clip(y * 32767.0, -32768, 32767).astype("<i2")
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


# --------------------------------------------------------------------------
# 素材 1：贝斯（原文最小的那组合成测试）
# --------------------------------------------------------------------------

BASS_PITCHES = [40, 43, 45, 47, 48, 50, 52, 55, 57, 59, 60, 62, 64]
BASS_AMPS_DB = [-3.0, -9.0, -6.0, -14.0, -4.0, -11.0, -7.0, -2.0,
                -12.0, -8.0, -5.0, -13.0, -10.0]


def make_bass_fixture(out_dir: str, delay_ms: float = 180.0,
                      fake_velocity: int = 53, jitter_ms: float = 13.0) -> dict:
    """返回期望值字典，供自动化测试断言。"""
    os.makedirs(out_dir, exist_ok=True)
    step = 0.5                      # 120BPM，四分音符
    dur = 0.36
    rng = np.random.default_rng(3)

    starts = [i * step + float(rng.uniform(0, jitter_ms)) / 1000.0
              for i in range(len(BASS_PITCHES))]
    total = starts[-1] + dur + 2.0
    y = np.zeros(int(total * SR), dtype=np.float32)

    for p, a_db, s in zip(BASS_PITCHES, BASS_AMPS_DB, starts):
        render_tone(y, SR, s + delay_ms / 1000.0, dur,
                    440.0 * 2 ** ((p - 69) / 12.0), 10 ** (a_db / 20.0))

    pm = pretty_midi.PrettyMIDI(initial_tempo=120.0)
    inst = pretty_midi.Instrument(program=33, name="Bass")
    for p, s in zip(BASS_PITCHES, starts):
        inst.notes.append(pretty_midi.Note(velocity=fake_velocity, pitch=p,
                                           start=s, end=s + dur))
    # 重复音：与第一个音同音高、起点差 8ms
    dup_start = starts[0] + 0.008
    inst.notes.append(pretty_midi.Note(velocity=fake_velocity, pitch=BASS_PITCHES[0],
                                       start=dup_start, end=dup_start + dur))
    # 哑音：MIDI 里有、WAV 里根本没响（放在尾部静音区）
    silent_start = starts[-1] + 1.6
    inst.notes.append(pretty_midi.Note(velocity=fake_velocity, pitch=45,
                                       start=silent_start, end=silent_start + 0.3))
    pm.instruments.append(inst)

    mid_path = os.path.join(out_dir, "TestSong (Bass).mid")
    wav_path = os.path.join(out_dir, "TestSong (Bass).wav")
    pm.write(mid_path)
    save_wav(wav_path, y)

    return {
        "midi": mid_path, "wav": wav_path, "name": "bass",
        "expected_offset_ms": delay_ms,
        "expected_notes_out": len(BASS_PITCHES),
        "midi_notes_in": len(BASS_PITCHES) + 2,
        "expected_duplicates": 1,
        "expected_silent": 1,
        "expected_real_pitches": BASS_PITCHES,
        "min_velocity_distinct": 8,
    }


# --------------------------------------------------------------------------
# 素材 2：安静前奏（-42dB）vs 主歌（-24dB）
# --------------------------------------------------------------------------

def make_quiet_intro_fixture(out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    intro_db, main_db = -42.0, -24.0
    intro_n, main_n = 20, 20
    step, dur = 0.5, 0.34
    intro_pitch = [52, 55, 57, 59, 60, 62, 64, 62, 60, 59,
                   57, 55, 52, 55, 57, 60, 62, 64, 65, 64]
    main_pitch = [48, 52, 55, 48, 50, 53, 57, 50, 52, 55,
                  59, 52, 53, 57, 60, 53, 55, 59, 62, 55]

    starts_i = [i * step for i in range(intro_n)]
    starts_m = [intro_n * step + 1.0 + i * step for i in range(main_n)]
    total = starts_m[-1] + dur + 1.5
    y = np.zeros(int(total * SR), dtype=np.float32)

    for p, s in zip(intro_pitch, starts_i):
        render_tone(y, SR, s, dur, 440.0 * 2 ** ((p - 69) / 12.0), 10 ** (intro_db / 20.0))
    for p, s in zip(main_pitch, starts_m):
        render_tone(y, SR, s, dur, 440.0 * 2 ** ((p - 69) / 12.0), 10 ** (main_db / 20.0))

    pm = pretty_midi.PrettyMIDI(initial_tempo=120.0)
    inst = pretty_midi.Instrument(program=0, name="Piano")
    for p, s in zip(intro_pitch + main_pitch, starts_i + starts_m):
        inst.notes.append(pretty_midi.Note(velocity=53, pitch=p, start=s, end=s + dur))
    pm.instruments.append(inst)

    mid_path = os.path.join(out_dir, "QuietIntro (Piano).mid")
    wav_path = os.path.join(out_dir, "QuietIntro (Piano).wav")
    pm.write(mid_path)
    save_wav(wav_path, y)

    return {
        "midi": mid_path, "wav": wav_path, "name": "quietintro",
        "expected_offset_ms": 0.0,
        "expected_notes_out": intro_n + main_n,
        "expected_deleted": 0,
        "intro_db": intro_db, "main_db": main_db,
    }


# --------------------------------------------------------------------------
# 素材 3：人声串音
# --------------------------------------------------------------------------

VOCAL_PITCHES = [60, 62, 64, 65, 67, 69, 71, 72, 67, 64, 62, 60]
VOCAL_TIMES = [1.0 + i * 0.5 for i in range(len(VOCAL_PITCHES))]
BASS_PITCHES_V = [41, 41, 45, 43, 41, 41, 45, 43,
                  41, 41, 45, 43, 41, 41, 45, 43]
BLEED_A = {"index": 3, "pitch": 78, "why": "无关音高"}
BLEED_B = {"index": 7, "pitch": 36, "why": "八度下方低音"}


def make_vocal_fixture(out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    total = VOCAL_TIMES[-1] + 1.6
    y = np.zeros(int(total * SR), dtype=np.float32)

    for p, s in zip(VOCAL_PITCHES, VOCAL_TIMES):
        render_vowel(y, SR, s, 0.44, 440.0 * 2 ** ((p - 69) / 12.0),
                     10 ** (-12.0 / 20.0))
    # 伴奏串进人声分轨（低频贝斯）
    for i, p in enumerate(BASS_PITCHES_V):
        render_tone(y, SR, 0.8 + i * 0.45, 0.4, 440.0 * 2 ** ((p - 69) / 12.0),
                    10 ** (-20.0 / 20.0), n_harm=4)
    add_noise_floor(y, -52.0)

    pm = pretty_midi.PrettyMIDI(initial_tempo=120.0)

    vocal = pretty_midi.Instrument(program=53, name="Vocal")
    for p, s in zip(VOCAL_PITCHES, VOCAL_TIMES):
        vocal.notes.append(pretty_midi.Note(velocity=53, pitch=p, start=s, end=s + 0.44))
    for b in (BLEED_A, BLEED_B):
        s = VOCAL_TIMES[b["index"]]
        vocal.notes.append(pretty_midi.Note(velocity=53, pitch=b["pitch"],
                                            start=s, end=s + 0.3))
    pm.instruments.append(vocal)

    bass = pretty_midi.Instrument(program=33, name="Bass")
    for i, p in enumerate(BASS_PITCHES_V):
        s = 0.8 + i * 0.45
        bass.notes.append(pretty_midi.Note(velocity=53, pitch=p, start=s, end=s + 0.4))
    pm.instruments.append(bass)

    # 人声与贝斯同一条分轨内的两种乐器混在一起时，Suno 会分别导出；
    # 这里把"人声 WAV"做成含串音的真实素材。
    mid_path = os.path.join(out_dir, "Song (Vocal).mid")
    wav_path = os.path.join(out_dir, "Song (Vocal).wav")
    pm.write(mid_path)
    save_wav(wav_path, y)

    return {
        "midi": mid_path, "wav": wav_path, "name": "vocal",
        "expected_offset_ms": 0.0,
        "vocal_notes_in": len(VOCAL_PITCHES) + 2,
        "vocal_notes_out": len(VOCAL_PITCHES),
        "bass_notes_out": len(BASS_PITCHES_V),
        "expected_vocal_removed": 2,
        "bleeds": [BLEED_A, BLEED_B],
    }


def make_all(root: str) -> dict:
    return {
        "bass": make_bass_fixture(os.path.join(root, "bass")),
        "quietintro": make_quiet_intro_fixture(os.path.join(root, "quietintro")),
        "vocal": make_vocal_fixture(os.path.join(root, "vocal")),
    }


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else "fixtures"
    made = make_all(root)
    for k, v in made.items():
        print("[%s] %s" % (k, v["wav"]))
    print("已生成 3 组测试素材。")
