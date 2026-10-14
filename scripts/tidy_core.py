#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Suno MIDI 整音 —— 核心引擎

把 Suno 导出的分轨 WAV / MIDI 配对整理成可用于出谱与继续编曲的文件。

设计原则（源自熊野ぴ太郎的开发日志）：
  1. MIDI 自带 Velocity 与真实音频几乎不相关 -> 一律从 WAV 重建，不采信原始数值。
  2. 判断"该不该删"只能用 WAV 里真实存在的声音，且必须用**局部**窗口，
     不能用整首歌最响处当基准（否则安静前奏会被整段误删）。
  3. 任何"清理"都必须留下记录，误删比不删更糟。
  4. 时间对齐必须放在力度与清理之前，否则力度会被记到错误的音符头上。

依赖：numpy（必需）、pretty_midi（必需）、scipy/soundfile（可选，缺省自动降级）
"""

from __future__ import annotations

import copy
import csv
import json
import math
import os
import re
import wave
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

try:                                  # 同目录导入（脚本方式 / 包方式都兼容）
    from . import licensing           # type: ignore
except Exception:                     # pragma: no cover
    import licensing                  # type: ignore

EPS = 1e-12
PITCH_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def pitch_name(midi_pitch: int) -> str:
    """60 -> 'C4'（以 C4=MIDI 60 为基准）"""
    return "%s%d" % (PITCH_NAMES[int(midi_pitch) % 12], int(midi_pitch) // 12 - 1)


# --------------------------------------------------------------------------
# 音频读取 / 包络
# --------------------------------------------------------------------------

def load_audio_mono(path: str) -> Tuple[np.ndarray, int]:
    """读取音频为单声道 float32。

    优先 soundfile（支持 wav/flac/ogg/aiff），失败则退回标准库 wave（16/24/32bit PCM）。
    """
    try:
        import soundfile as sf  # type: ignore

        data, sr = sf.read(path, dtype="float32", always_2d=True)
        y = data.mean(axis=1).astype(np.float32)
        return y, int(sr)
    except Exception:
        return _load_wav_stdlib(path)


def _load_wav_stdlib(path: str) -> Tuple[np.ndarray, int]:
    with wave.open(path, "rb") as wf:
        sr = wf.getframerate()
        n_ch = wf.getnchannels()
        width = wf.getsampwidth()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    if width == 1:
        a = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = (b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16))
        v = np.where(v & 0x800000, v - 0x1000000, v)
        a = v.astype(np.float32) / 8388608.0
    elif width == 4:
        a = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError("不支持的 WAV 位深: %d bit" % (width * 8))

    if n_ch > 1:
        a = a.reshape(-1, n_ch).mean(axis=1)
    return a.astype(np.float32), int(sr)


def rms_envelope(y: np.ndarray, frame: int, hop: int) -> np.ndarray:
    """分块短时 RMS，避免一次性物化巨型中间数组。"""
    n = len(y)
    if n < frame:
        return np.array([float(np.sqrt(np.mean(y.astype(np.float64) ** 2) + EPS))], dtype=np.float64)
    n_frames = 1 + (n - frame) // hop
    out = np.empty(n_frames, dtype=np.float64)
    chunk = 4096
    for s in range(0, n_frames, chunk):
        e = min(n_frames, s + chunk)
        idx = np.arange(s, e)[:, None] * hop + np.arange(frame)[None, :]
        seg = y[idx].astype(np.float64)
        out[s:e] = np.sqrt(np.mean(seg * seg, axis=1) + EPS)
    return out


def db(x: np.ndarray) -> np.ndarray:
    return 20.0 * np.log10(np.maximum(x, 1e-9))


def onset_strength(env_db: np.ndarray, smooth: int = 3) -> np.ndarray:
    """能量包络的正向差分（半波整流），即"声音突然变强"的位置。"""
    if len(env_db) < 2:
        return np.zeros_like(env_db)
    d = np.diff(env_db, prepend=env_db[0])
    d = np.maximum(d, 0.0)
    if smooth > 1 and len(d) >= smooth:
        k = np.ones(smooth) / smooth
        d = np.convolve(d, k, mode="same")
    return d


def zscore(a: np.ndarray) -> np.ndarray:
    if len(a) == 0:
        return a
    m = float(np.mean(a))
    s = float(np.std(a))
    if s < 1e-12:
        return np.zeros_like(a)
    return (a - m) / s


def onset_envelope(y: np.ndarray, frame: int = 512, hop: int = 128) -> np.ndarray:
    """起音检测专用能量包络。

    关键细节：在信号前补 (frame - hop) 个零。窗口是"滑入"音符的，若不补偿，
    检测到的起音会系统性提前 (frame-hop)/hop 帧（512/128 时实测约 3 帧 = 17ms，
    在 120BPM 下已经是三分之一拍）。补零后窗口右端与帧栅格对齐，偏差归零。
    """
    pad = np.zeros(max(0, frame - hop), dtype=np.float32)
    return rms_envelope(np.concatenate([pad, y]), frame, hop)


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------

@dataclass
class NoteRec:
    pitch: int
    start: float          # 已应用对齐偏移后的时间（秒）
    end: float
    velocity: int         # 当前（重建后）力度
    orig_velocity: int    # 原始力度
    orig_index: int
    keep: bool = True
    reason: str = ""      # 删除/修改原因
    note_db: float = float("nan")
    local_ref_db: float = float("nan")
    local_noise_db: float = float("nan")
    snr_db: float = float("nan")
    orig_start: float = 0.0
    polyphonic_uncertain: bool = False

    @property
    def dur(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class TrackRec:
    index: int
    name: str
    program: int
    is_drum: bool
    is_vocal: bool
    notes: List[NoteRec] = field(default_factory=list)
    offset_ms: float = float("nan")
    offset_corr: float = float("nan")

    @property
    def label(self) -> str:
        base = self.name or ("Track %d" % (self.index + 1))
        if self.is_drum:
            return base + " [drums]"
        return base


@dataclass
class TidyConfig:
    out_dir: str = "tidy_out"
    max_offset_ms: float = 450.0
    ambiguity_ratio: float = 0.90
    apply_align: bool = True
    align_mode: str = "midi"          # midi | none
    dup_tol_ms: float = 35.0
    delete_duplicates: bool = True
    delete_silent: bool = True
    snr_db_min: float = 4.0           # 保守：明显没声才删
    silent_rel_db: float = 12.0       # 还要明显弱于邻近音符才删（双条件 AND）
    note_win_ms: float = 90.0         # 力度取样窗 + 有声音符判据窗
    context_sec: float = 4.0          # 局部上下文（原文：前后约四秒）
    ref_percentile: float = 90.0      # 局部基准取邻近音符响度的高分位
    vel_min: int = 20
    vel_max: int = 127
    vel_range_db: float = 24.0
    vocal_check: str = "auto"         # auto | on | off
    vocal_min_evidence_db: float = 10.0   # 谐波相对同频段非谐波内容的余量
    octave_guard_db: float = -8.0     # 基频弱于二次谐波这么多 -> 判为八度下方误判
    grid_div: int = 4                 # 量化栅格 = 1/grid_div 分音符（4 => 十六分）
    time_sig: Optional[str] = None    # 例如 "3/4"
    emit_mix: bool = True
    emit_score: bool = True
    overwrite_guard: bool = True
    # ---- 能力集（当前为全集，字段保留以支持将来的分层） ----
    caps: Optional[frozenset] = None  # None = 不限制（引擎自测、内部调用）
    coarse_velocity: bool = False     # True = 力度压到粗阶梯（≤8 档）
    full_report: bool = True          # False = 不输出逐音删除清单
    audio_limit_s: Optional[float] = None   # 单次音频时长上限（秒）


# --------------------------------------------------------------------------
# 配对
# --------------------------------------------------------------------------

_AUDIO_EXT = (".wav", ".flac", ".ogg", ".aiff", ".aif")
_MIDI_EXT = (".mid", ".midi", ".smf")


def discover_pairs(folder: str) -> List[Tuple[str, str]]:
    """按同名主干配对 WAV 与 MIDI。

    返回 [(midi_path, wav_path), ...]。只要求主干一致，扩展名大小写不敏感。
    """
    files = [f for f in os.listdir(folder) if os.path.isfile(os.path.join(folder, f))]
    by_stem: Dict[str, Dict[str, str]] = {}
    for f in files:
        stem, ext = os.path.splitext(f)
        ext = ext.lower()
        if ext in _MIDI_EXT:
            by_stem.setdefault(stem, {})["midi"] = os.path.join(folder, f)
        elif ext in _AUDIO_EXT:
            by_stem.setdefault(stem, {})["wav"] = os.path.join(folder, f)

    pairs: List[Tuple[str, str]] = []
    for stem in sorted(by_stem):
        d = by_stem[stem]
        if "midi" in d and "wav" in d:
            pairs.append((d["midi"], d["wav"]))
    return pairs


# --------------------------------------------------------------------------
# 起音偏移检测
# --------------------------------------------------------------------------

def _impulse_train(times: Sequence[float], n_frames: int, sr: int, hop: int,
                   sigma_frames: float = 1.5) -> np.ndarray:
    tr = np.zeros(n_frames, dtype=np.float64)
    for t in times:
        i = int(round(t * sr / hop))
        if 0 <= i < n_frames:
            tr[i] += 1.0
    if sigma_frames > 0:
        r = int(math.ceil(3 * sigma_frames))
        k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma_frames) ** 2)
        k /= k.sum()
        tr = np.convolve(tr, k, mode="same")
    return tr


def _beat_frames(env: np.ndarray, sr: int, hop: int) -> float:
    """用自相关估计主周期（帧），用于识别"整拍歧义"（40–240 BPM 范围内）。"""
    n = len(env)
    if n < 64:
        return float("inf")
    x = env - env.mean()
    ac = np.correlate(x, x, mode="full")[n - 1:]
    ac = ac / (ac[0] + EPS)
    lo = max(2, int(60.0 / 240.0 * sr / hop))      # 240 BPM
    hi = min(len(ac) - 1, int(60.0 / 40.0 * sr / hop))  # 40 BPM
    if hi <= lo + 2:
        return float("inf")
    seg = ac[lo:hi]
    peak = int(np.argmax(seg)) + lo
    if peak <= 0:
        return float("inf")
    best = peak
    for m in (2, 3, 4):
        p = int(round(peak / m))
        if lo <= p < hi:
            w = max(1, peak // 20)
            s = ac[max(lo, p - w):min(hi, p + w + 1)]
            if len(s) and s.max() > 0.55 * ac[peak]:
                best = p
                break
    return float(best)


def _best_lag(env: np.ndarray, train: np.ndarray, max_lag_frames: int
              ) -> Tuple[np.ndarray, np.ndarray]:
    """score(L) = Σ env[t]·train[t-L]；L>0 表示音频比 MIDI 晚 L 帧。"""
    n = len(env)
    lags = np.arange(-max_lag_frames, max_lag_frames + 1)
    scores = np.zeros(len(lags), dtype=np.float64)
    for k, L in enumerate(lags):
        if L >= 0:
            a = env[L:]
            b = train[:n - L]
        else:
            a = env[:n + L]
            b = train[-L:]
        m = min(len(a), len(b))
        if m <= 1:
            continue
        scores[k] = float(np.dot(a[:m], b[:m]))
    return lags, scores


def estimate_offset(audio_env: np.ndarray, note_times: Sequence[float], sr: int,
                    hop: int, cfg: TidyConfig) -> Dict[str, float]:
    """估算 WAV 相对 MIDI 的起音延迟（毫秒，正=WAV 更晚）。"""
    n_frames = len(audio_env)
    if n_frames < 16 or not note_times:
        return {"offset_ms": 0.0, "corr": 0.0, "ambiguous_ms": float("nan"),
                "beat_ms": float("nan"), "raw_offset_ms": 0.0}

    env = zscore(onset_strength(db(audio_env)))
    train = _impulse_train(note_times, n_frames, sr, hop)
    train = zscore(train)

    max_lag = int(cfg.max_offset_ms / 1000.0 * sr / hop)
    if max_lag < 2:
        max_lag = 2
    lags, scores = _best_lag(env, train, max_lag)
    if not np.any(scores):
        return {"offset_ms": 0.0, "corr": 0.0, "ambiguous_ms": float("nan"),
                "beat_ms": float("nan"), "raw_offset_ms": 0.0}

    k_peak = int(np.argmax(scores))
    raw_lag = int(lags[k_peak])
    peak = float(scores[k_peak])

    beat = _beat_frames(env, sr, hop)
    beat_ms = beat * hop / sr * 1000.0 if np.isfinite(beat) else float("nan")

    chosen_lag = raw_lag
    ambiguous_ms = float("nan")
    if beat_ms == beat_ms and np.isfinite(beat_ms) and abs(raw_lag) > 0.6 * beat:
        thresh = cfg.ambiguity_ratio * peak
        # 在离 0 更近的位置找"几乎一样好"的峰，避免把整拍误差当成真偏移
        order = np.argsort(np.abs(lags))
        for k in order:
            if abs(int(lags[k])) >= abs(raw_lag):
                continue
            if scores[k] < thresh:
                continue
            lo = max(0, k - 1)
            hi = min(len(scores), k + 2)
            if scores[k] >= scores[lo:hi].max() - 1e-9:
                chosen_lag = int(lags[k])
                ambiguous_ms = raw_lag * hop / sr * 1000.0
                break

    return {
        "offset_ms": chosen_lag * hop / sr * 1000.0,
        "raw_offset_ms": raw_lag * hop / sr * 1000.0,
        "corr": peak,
        "ambiguous_ms": ambiguous_ms,
        "beat_ms": beat_ms,
        "hop": hop,
    }


# --------------------------------------------------------------------------
# 力度重建 / 清理
# --------------------------------------------------------------------------

def _note_window_db(y: np.ndarray, sr: int, start: float, win_s: float) -> float:
    i0 = int(max(0.0, start) * sr)
    i1 = int(min(len(y), i0 + max(1, int(win_s * sr))))
    if i1 <= i0:
        return -120.0
    seg = y[i0:i1].astype(np.float64)
    rms = math.sqrt(float(np.mean(seg * seg)) + EPS)
    return 20.0 * math.log10(max(rms, 1e-9))


def _local_stats(env_db: np.ndarray, sr: int, hop: int, start: float, ctx: float
                 ) -> Tuple[float, float]:
    fps = sr / float(hop)
    lo = max(0, int((start - ctx) * fps))
    hi = min(len(env_db), int((start + ctx) * fps) + 1)
    if hi - lo < 3:
        lo = 0
        hi = len(env_db)
    w = env_db[lo:hi]
    return float(np.percentile(w, 90)), float(np.percentile(w, 10))


def _looks_polyphonic(notes: List[NoteRec], tol_s: float = 0.04) -> None:
    """同轨近同时多音 -> 力度/an 置信度下降，标记出来供人工复核。"""
    by_start = sorted(range(len(notes)), key=lambda i: notes[i].start)
    for a, b in zip(by_start, by_start[1:]):
        if abs(notes[b].start - notes[a].start) <= tol_s and notes[a].pitch != notes[b].pitch:
            notes[a].polyphonic_uncertain = True
            notes[b].polyphonic_uncertain = True


def measure_levels(track: TrackRec, y: np.ndarray, sr: int, env_db: np.ndarray,
                   hop: int, cfg: TidyConfig) -> None:
    """第一遍：用同一口径（音符起点后 90ms 窗 RMS）测每个音符的实际电平。

    必须与"局部基准"用同一口径，否则会把"起音峰值"和"包络平均值"直接相减，
    结果所有音符的相对值都是正的、被顶到 0dB 上限（这是个很容易踩的坑）。
    """
    win = cfg.note_win_ms / 1000.0
    for n in track.notes:
        n.note_db = _note_window_db(y, sr, n.start, min(win, max(0.02, n.dur)))
        _, n.local_noise_db = _local_stats(env_db, sr, hop, n.start, cfg.context_sec)


def assign_local_ref(track: TrackRec, cfg: TidyConfig, peers_only_kept: bool = True) -> None:
    """局部基准 = 前后 context 秒内邻近音符电平的高分位。

    原文要点：不能用"整首歌最响的部分"当基准（否则 -42dB 的安静前奏会被整段
    认成杂音），要看它前后约四秒的声音。这里把它落成可复核的数值。
    """
    pool = [n for n in track.notes if (n.keep or not peers_only_kept)]
    for n in track.notes:
        peers = [m.note_db for m in pool if abs(m.start - n.start) <= cfg.context_sec
                 and m.note_db == m.note_db]
        if not peers:
            peers = [m.note_db for m in pool if m.note_db == m.note_db] or [n.note_db]
        if len(peers) >= 3:
            n.local_ref_db = float(np.percentile(peers, cfg.ref_percentile))
        else:
            n.local_ref_db = float(max(peers))


def rebuild_velocity(track: TrackRec, cfg: TidyConfig) -> None:
    """从 WAV 局部音量重建 Velocity（原文核心修正：不采信原始力度）。

    原文实测：某贝斯轨 1056 个音里 86% 的 Velocity 都是 53，只有 7 种数值，
    与对应 WAV 音量的相关系数 -0.001 —— 原始力度数值基本不含演奏信息。
    """
    for n in track.notes:
        if not n.keep:
            continue
        rel = n.note_db - n.local_ref_db          # 相对本地乐段，而非整首歌
        rel = max(-cfg.vel_range_db, min(0.0, rel))
        frac = (rel + cfg.vel_range_db) / cfg.vel_range_db
        vel = int(round(cfg.vel_min + (cfg.vel_max - cfg.vel_min) * frac))
        vel = max(1, min(127, vel))
        if cfg.coarse_velocity:                   # 粗力度模式：压到 8 档阶梯
            vel = licensing.coarsen_velocity(vel)
        n.velocity = vel


def remove_duplicates(track: TrackRec, y: np.ndarray, sr: int, cfg: TidyConfig) -> int:
    """同音高、几乎同时触发 -> 只留证据最强的一个。"""
    if not cfg.delete_duplicates:
        return 0
    removed = 0
    tol = cfg.dup_tol_ms / 1000.0
    alive = [n for n in track.notes if n.keep]
    for i in range(len(alive)):
        a = alive[i]
        if not a.keep:
            continue
        for j in range(i + 1, len(alive)):
            b = alive[j]
            if not b.keep:
                continue
            if b.pitch != a.pitch:
                continue
            if abs(b.start - a.start) > tol:
                continue
            ea = _note_window_db(y, sr, a.start, min(0.09, max(0.02, a.dur)))
            eb = _note_window_db(y, sr, b.start, min(0.09, max(0.02, b.dur)))
            victim = b if ea >= eb - 0.5 else a     # 平手时保留更早的那个
            victim.keep = False
            victim.reason = "duplicate(same pitch %.0fms apart)" % (abs(b.start - a.start) * 1000)
            removed += 1
            if victim is a:
                break
    return removed


def remove_silent(track: TrackRec, cfg: TidyConfig) -> int:
    """只删"WAV 里确实没有声音"的音符；保留全部删除记录。

    双条件 AND，避免把安静乐段里的弱音误删：
      ① 局部信噪比低于阈值（相对前后 context 秒的底噪）
      ② 同时明显弱于邻近音符（相对局部基准）
    """
    if not cfg.delete_silent:
        return 0
    removed = 0
    for n in track.notes:
        if not n.keep:
            continue
        n.snr_db = n.note_db - n.local_noise_db
        rel = n.note_db - n.local_ref_db
        if n.snr_db < cfg.snr_db_min and rel < -cfg.silent_rel_db:
            n.keep = False
            n.reason = "no-audio(snr %.1fdB, %.1fdB below peers)" % (n.snr_db, -rel)
            removed += 1
    return removed


# --------------------------------------------------------------------------
# 人声核验
# --------------------------------------------------------------------------

_VOCAL_RE = re.compile(
    r"(vocal|voice|vox|lead\s*vocal|人声|主唱|唱|歌声|vocalist|backing)", re.IGNORECASE)


def is_vocal_track(name: str, program: int, is_drum: bool) -> bool:
    if is_drum:
        return False
    if name and _VOCAL_RE.search(name):
        return True
    return False


def _vocal_features(y: np.ndarray, sr: int, start: float, f0: float,
                    n_fft: int = 8192) -> Tuple[float, float, float]:
    """人声音高证据。

    返回 (evidence_db, fund_vs_h2_db, half_ratio_db)。

    evidence_db = 目标音的谐波能量相对**同一频段内非谐波内容第 95 百分位**的余量。
    这一点踩过坑：若用"整个 200–4000Hz 的中位谱"当参考，频谱倾斜会让
    "根本不存在该音高"的假音符也拿到 25–35dB 的高分，判据完全失效。
    改成同频段非谐波内容做参考后，实测真实音 19.5–30.6dB、假音 -12.9dB，
    10dB 阈值有很宽的隔离带。
    """
    i0 = int(max(0.0, start) * sr)
    i1 = min(len(y), i0 + max(1024, int(0.09 * sr)))
    if i1 - i0 < 512:
        return -120.0, 0.0, 0.0
    seg = y[i0:i1].astype(np.float64)
    seg = seg - seg.mean()
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg)), n=n_fft))
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
    bin_hz = sr / float(n_fft)

    def band_max(f: float) -> float:
        if f <= 0 or f >= sr / 2:
            return 0.0
        tol = max(0.025, 1.5 * bin_hz / f)       # 低频容差自适应，否则 65Hz 落不进一个频点
        m = (freqs >= f * (1 - tol)) & (freqs <= f * (1 + tol))
        return float(spec[m].max()) if np.any(m) else 0.0

    h1, h2, h3 = band_max(f0), band_max(2 * f0), band_max(3 * f0)
    num = (h1 + 0.6 * h2 + 0.4 * h3) / 2.0

    lo, hi = 0.5 * f0, min(sr * 0.49, 3.5 * f0)
    m = (freqs >= lo) & (freqs <= hi)
    keep = np.ones(freqs.shape, dtype=bool)
    for k in (1, 2, 3):
        f = f0 * k
        tol = max(0.025, 3.0 * bin_hz / f)
        keep &= ~((freqs >= f * (1 - tol)) & (freqs <= f * (1 + tol)))
    resid = spec[m & keep]
    if resid.size == 0:
        return 20.0 * math.log10(max(num, EPS)), 0.0, 0.0
    floor = float(np.percentile(resid, 95))
    evidence = 20.0 * math.log10((num + EPS) / (floor + EPS))
    fund_vs_h2 = 20.0 * math.log10((h1 + EPS) / (h2 + EPS))
    half = band_max(f0 / 2.0)
    half_ratio = 20.0 * math.log10((half + EPS) / (h1 + EPS))
    return evidence, fund_vs_h2, half_ratio


def vocal_verify(track: TrackRec, y: np.ndarray, sr: int, cfg: TidyConfig) -> int:
    """人声轨：只保留能在频谱里找到该音高（及倍频）证据的音符。

    解决原文两个问题：① 伴奏串进主唱轨；② 音量检测对"同时响起的假音符"失效
    （歌手正在唱时，假音符所在位置同样有声音，光看音量会放它过关）。
    """
    removed = 0
    for n in track.notes:
        if not n.keep:
            continue
        f0 = 440.0 * (2.0 ** ((n.pitch - 69) / 12.0))
        ev, fund_vs_h2, half_ratio = _vocal_features(y, sr, n.start, f0)
        n.snr_db = ev

        if ev < cfg.vocal_min_evidence_db:
            n.keep = False
            n.reason = "vocal-no-pitch-evidence(%.1fdB)" % ev
            removed += 1
            continue
        # 八度下方误判：写低了八度时基频必然远弱于二次谐波
        if fund_vs_h2 < cfg.octave_guard_db:
            n.keep = False
            n.reason = "vocal-octave-below(fund %.1fdB vs 2nd harm)" % fund_vs_h2
            removed += 1
            continue
        # 八度上方误判：更低的次谐波反而更强 -> 只报告，不自动改音高
        if half_ratio > 6.0:
            n.reason = (n.reason + "; suspect-octave-up").strip("; ")
    return removed


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------

@dataclass
class TrackReport:
    name: str
    notes_in: int
    notes_out: int
    duplicates_removed: int
    silent_removed: int
    vocal_removed: int
    vel_distinct_before: int
    vel_distinct_after: int
    vel_min: int
    vel_max: int
    offset_ms: float
    polyphonic_uncertain: int
    is_vocal: bool
    is_drum: bool


@dataclass
class PairReport:
    midi_path: str
    wav_path: str
    offset_ms: float
    offset_corr: float
    raw_offset_ms: float
    ambiguous_ms: float
    beat_ms: float
    applied_offset_ms: float
    duration_s: float
    tracks: List[TrackReport] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    removed_notes: List[Dict[str, object]] = field(default_factory=list)
    # 内部句柄：供 emit_pair 二次写出，不参与 JSON 序列化
    _pm: object = field(default=None, repr=False, compare=False)
    _tracks: Optional[List[TrackRec]] = field(default=None, repr=False, compare=False)

    @property
    def stem(self) -> str:
        return os.path.splitext(os.path.basename(self.midi_path))[0]


def _write_time_signature(path: str, time_sig: str) -> bool:
    """用 mido 写入拍号（pretty_midi 不产出 time_signature）。"""
    m = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*$", time_sig)
    if not m:
        return False
    num, den = int(m.group(1)), int(m.group(2))
    try:
        import mido  # type: ignore

        mid = mido.MidiFile(path)
        for tr in mid.tracks:
            tr[:] = [msg for msg in tr if msg.type != "time_signature"]
        target = mid.tracks[0] if mid.tracks else None
        if target is None:
            return False
        msg = mido.MetaMessage("time_signature", numerator=num, denominator=den, time=0)
        target.insert(1 if len(target) else 0, msg)
        mid.save(path)
        return True
    except Exception:
        return False


def _quantize_tick(pm, tick: int, div: int) -> int:
    grid = max(1, int(round(pm.resolution / float(div))))
    return int(round(tick / grid)) * grid


def build_output(pm, tracks: List[TrackRec], target: str, cfg: TidyConfig):
    """target: 'mix'（保留演奏时间细节） | 'score'（按栅格量化，适合 MuseScore）"""
    out = copy.deepcopy(pm)
    out.instruments = []
    import pretty_midi  # 局部导入，便于无依赖时报错清晰

    for trk in tracks:
        inst = pretty_midi.Instrument(program=trk.program, is_drum=trk.is_drum,
                                      name=trk.name or "")
        for n in trk.notes:
            if not n.keep:
                continue
            if target == "score":
                t0 = _quantize_tick(pm, pm.time_to_tick(n.start), cfg.grid_div)
                t1 = _quantize_tick(pm, pm.time_to_tick(n.end), cfg.grid_div)
                grid = max(1, int(round(pm.resolution / float(cfg.grid_div))))
                if t1 <= t0:
                    t1 = t0 + grid
                s = pm.tick_to_time(t0)
                e = pm.tick_to_time(t1)
            else:
                s, e = n.start, max(n.end, n.start + 0.01)
            inst.notes.append(pretty_midi.Note(velocity=int(n.velocity),
                                               pitch=int(n.pitch),
                                               start=float(s), end=float(e)))
        out.instruments.append(inst)
    return out


def write_reports(pair: PairReport, out_dir: str,
                  full_report: bool = True) -> Tuple[str, str, str]:
    rdir = os.path.join(out_dir, "report")
    os.makedirs(rdir, exist_ok=True)
    stem = pair.stem

    csv_path = os.path.join(rdir, stem + "_report.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["轨道", "原始音符", "保留音符", "重复音删除", "无声音符删除",
                    "人声串音删除", "力度种类(前)", "力度种类(后)", "力度范围",
                    "起音偏移(ms)", "复调不确定音", "类型"])
        for t in pair.tracks:
            kind = "鼓组" if t.is_drum else ("人声" if t.is_vocal else "乐器")
            w.writerow([t.name, t.notes_in, t.notes_out, t.duplicates_removed,
                        t.silent_removed, t.vocal_removed, t.vel_distinct_before,
                        t.vel_distinct_after, "%d-%d" % (t.vel_min, t.vel_max),
                        "%.0f" % t.offset_ms, t.polyphonic_uncertain, kind])

    # full_report=False 时只输出汇总，但仍把删除原因聚合进 summary
    rem_path = ""
    if full_report:
        rem_path = os.path.join(rdir, stem + "_removed_notes.csv")
        with open(rem_path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["轨道", "音名", "MIDI音高", "原始起点(s)", "时长(ms)",
                        "原始力度", "本地参考(dB)", "本地底噪(dB)", "SNR(dB)", "删除原因"])
            for r in pair.removed_notes:
                w.writerow([r["track"], r["pitch_name"], r["pitch"], "%.3f" % r["start"],
                            "%.0f" % r["dur_ms"], r["orig_velocity"],
                            _fmt(r["local_ref_db"]), _fmt(r["local_noise_db"]),
                            _fmt(r["snr_db"]), r["reason"]])

    json_path = os.path.join(rdir, stem + "_summary.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump({
            "midi": os.path.basename(pair.midi_path),
            "wav": os.path.basename(pair.wav_path),
            "offset": {
                "chosen_ms": round(pair.offset_ms, 2),
                "raw_peak_ms": round(pair.raw_offset_ms, 2),
                "applied_ms": round(pair.applied_offset_ms, 2),
                "correlation": round(pair.offset_corr, 4),
                "ambiguous_beat_lag_ms": (None if pair.ambiguous_ms != pair.ambiguous_ms
                                          else round(pair.ambiguous_ms, 2)),
                "beat_period_ms": (None if pair.beat_ms != pair.beat_ms else round(pair.beat_ms, 2)),
            },
            "duration_s": round(pair.duration_s, 3),
            "tracks": [t.__dict__ for t in pair.tracks],
            "removed_notes_total": len(pair.removed_notes),
            "removed_reasons": _reason_histogram(pair.removed_notes),
            "full_report": bool(full_report),
            "warnings": pair.warnings,
        }, fh, ensure_ascii=False, indent=2)
    return csv_path, rem_path, json_path


def _reason_histogram(rows: List[Dict[str, object]]) -> Dict[str, int]:
    """把删除原因归一成前缀计数（不输出逐音清单时，至少能看到删了什么类型）。"""
    hist: Dict[str, int] = {}
    for r in rows:
        reason = str(r.get("reason") or "")
        key = reason.split("(")[0].split(";")[0].strip() or "unknown"
        hist[key] = hist.get(key, 0) + 1
    return hist


def _fmt(v: object) -> str:
    try:
        f = float(v)  # type: ignore[arg-type]
    except Exception:
        return ""
    return "" if f != f else "%.1f" % f


def print_report(pair: PairReport) -> None:
    off = pair.offset_ms
    print("\n=== %s ===" % pair.stem)
    if pair.raw_offset_ms != pair.offset_ms:
        print("起音偏移: WAV 比 MIDI 晚 %.0f ms (原始峰值 %.0f ms 疑为整拍误差)"
              % (off, pair.raw_offset_ms))
    else:
        print("起音偏移: WAV 比 MIDI 晚 %.0f ms  (相关性 %.3f)" % (off, pair.offset_corr))
    if pair.beat_ms == pair.beat_ms:
        print("          估计拍周期 %.0f ms -> %.0f ms 约为 %.2f 拍"
              % (pair.beat_ms, abs(off), abs(off) / max(1e-6, pair.beat_ms)))
    print("已应用对齐: %+.0f ms (MIDI 时间轴)" % pair.applied_offset_ms)
    print("%-22s %8s %8s %8s %8s %10s" % ("轨道", "原始", "保留", "重删", "哑删", "力度种类"))
    for t in pair.tracks:
        print("%-22s %8d %8d %8d %8d %6d→%-4d" % (
            t.name[:22], t.notes_in, t.notes_out, t.duplicates_removed,
            t.silent_removed + t.vocal_removed, t.vel_distinct_before, t.vel_distinct_after))
    for w in pair.warnings:
        print("! %s" % w)


# --------------------------------------------------------------------------
# 主流水线
# --------------------------------------------------------------------------

def emit_pair(pair: PairReport, cfg: TidyConfig) -> None:
    """把分析结果写出为 mix / score / report。"""
    out_dir = cfg.out_dir
    os.makedirs(out_dir, exist_ok=True)
    pm = pair._pm
    tracks = pair._tracks or []
    if cfg.emit_mix:
        d = os.path.join(out_dir, "mix")
        os.makedirs(d, exist_ok=True)
        outp = os.path.join(d, pair.stem + ".mid")
        build_output(pm, tracks, "mix", cfg).write(outp)
        if cfg.time_sig:
            _write_time_signature(outp, cfg.time_sig)
    if cfg.emit_score:
        d = os.path.join(out_dir, "score")
        os.makedirs(d, exist_ok=True)
        outp = os.path.join(d, pair.stem + ".mid")
        build_output(pm, tracks, "score", cfg).write(outp)
        if cfg.time_sig:
            _write_time_signature(outp, cfg.time_sig)
    write_reports(pair, out_dir, full_report=cfg.full_report)


def process_pair(midi_path: str, wav_path: str, cfg: TidyConfig,
                 out_dir: str, log=print, emit: bool = True) -> PairReport:
    import pretty_midi

    if cfg.overwrite_guard:
        for p in (midi_path, wav_path):
            if os.path.abspath(p).startswith(os.path.abspath(out_dir) + os.sep):
                raise RuntimeError("输出目录不能位于输入文件所在目录内，防止覆盖原始文件")

    # 能力门禁（幂等，重复调用无副作用）；能力集为全集时此处不做任何改动
    if cfg.caps is not None:
        licensing.apply_caps(cfg, cfg.caps)

    y, sr = load_audio_mono(wav_path)
    licensing.check_duration(len(y) / float(sr), cfg)
    pm = pretty_midi.PrettyMIDI(midi_path)

    onset_hop = 128                       # 起音检测用（窗口右端对齐，见 onset_envelope）
    env_onset = onset_envelope(y, 512, onset_hop)
    loud_hop = 512                        # 响度统计用（帧较长，平滑无害）
    env_loud_db = db(rms_envelope(y, 1024, loud_hop))

    # ---- 1. 起音偏移（逐轨估计 + 中值投票） ----
    tmp_tracks: List[TrackRec] = []
    for ti, inst in enumerate(pm.instruments):
        if not inst.notes:
            continue
        tmp_tracks.append(TrackRec(index=ti, name=inst.name or "",
                                   program=inst.program, is_drum=inst.is_drum,
                                   is_vocal=is_vocal_track(inst.name or "", inst.program,
                                                           inst.is_drum),
                                   notes=[NoteRec(pitch=n.pitch, start=n.start, end=n.end,
                                                  velocity=n.velocity,
                                                  orig_velocity=n.velocity,
                                                  orig_index=ni, orig_start=n.start)
                                          for ni, n in enumerate(inst.notes)]))

    per_track = []
    for t in tmp_tracks:
        res = estimate_offset(env_onset, [n.orig_start for n in t.notes], sr, onset_hop, cfg)
        t.offset_ms, t.offset_corr = res["offset_ms"], res["corr"]
        per_track.append((t, res))

    good = [(t, r) for t, r in per_track if r["corr"] > 0]
    if good:
        best_corr = max(r["corr"] for _, r in good)
        strong = [(t, r) for t, r in good if r["corr"] >= 0.25 * best_corr]
        chosen = float(np.median([r["offset_ms"] for _, r in strong]))
        raw = float(np.median([r["raw_offset_ms"] for _, r in strong]))
        corr = float(max(r["corr"] for _, r in strong))
        amb = [r["ambiguous_ms"] for _, r in strong if r["ambiguous_ms"] == r["ambiguous_ms"]]
        beat = strong[0][1]["beat_ms"]
    else:
        chosen = raw = corr = 0.0
        amb = []
        beat = float("nan")

    applied = chosen if (cfg.apply_align and cfg.align_mode == "midi") else 0.0

    # ---- 2. 应用对齐（之后再算力度，原文明确要求） ----
    for t in tmp_tracks:
        for n in t.notes:
            n.start = n.orig_start + applied / 1000.0
            n.end = n.end + applied / 1000.0
            if n.start < 0:
                n.end -= n.start
                n.start = 0.0

    # ---- 3. 清理 ----
    warnings: List[str] = []
    removed_rows: List[Dict[str, object]] = []
    track_reports: List[TrackReport] = []

    for t in tmp_tracks:
        notes_in = len(t.notes)
        v_before = len({n.orig_velocity for n in t.notes})

        # 电平测量统一口径 -> 用全部音符定"局部基准"（供哑音判据）
        measure_levels(t, y, sr, env_loud_db, loud_hop, cfg)
        assign_local_ref(t, cfg, peers_only_kept=False)

        dup = remove_duplicates(t, y, sr, cfg)
        sil = remove_silent(t, cfg)
        voc = 0
        if t.is_vocal and cfg.vocal_check != "off":
            voc = vocal_verify(t, y, sr, cfg)

        # 删除后再用**保留下来的**音符重定基准，再重建力度
        assign_local_ref(t, cfg, peers_only_kept=True)
        rebuild_velocity(t, cfg)
        _looks_polyphonic(t.notes)

        kept = [n for n in t.notes if n.keep]
        for n in t.notes:
            if not n.keep:
                removed_rows.append({
                    "track": t.label, "pitch": n.pitch, "pitch_name": pitch_name(n.pitch),
                    "start": n.start, "dur_ms": n.dur * 1000.0,
                    "orig_velocity": n.orig_velocity, "local_ref_db": n.local_ref_db,
                    "local_noise_db": n.local_noise_db, "snr_db": n.snr_db,
                    "reason": n.reason, "poly": n.polyphonic_uncertain,
                })

        if t.is_vocal and kept and voc > 0:
            pass
        if kept and v_before > 1 and len({n.orig_velocity for n in kept}) == v_before:
            pass
        vels = [n.velocity for n in kept]
        track_reports.append(TrackReport(
            name=t.label, notes_in=notes_in, notes_out=len(kept),
            duplicates_removed=dup, silent_removed=sil, vocal_removed=voc,
            vel_distinct_before=v_before,
            vel_distinct_after=len(set(vels)),
            vel_min=min(vels) if vels else 0, vel_max=max(vels) if vels else 0,
            offset_ms=t.offset_ms,
            polyphonic_uncertain=sum(1 for n in kept if n.polyphonic_uncertain),
            is_vocal=t.is_vocal, is_drum=t.is_drum))

        if notes_in and len(kept) / float(notes_in) < 0.5:
            warnings.append("%s: 删除了超过一半音符（%d/%d），请务必回听被删段落"
                            % (t.label, notes_in - len(kept), notes_in))
        if any(n.polyphonic_uncertain for n in kept) and not t.is_drum:
            warnings.append("%s: 存在同轨近同时多音，力度取自混合音频，需人工复核"
                            % t.label)

    pair = PairReport(midi_path=midi_path, wav_path=wav_path,
                      offset_ms=chosen, raw_offset_ms=raw, offset_corr=corr,
                      ambiguous_ms=(amb[0] if amb else float("nan")),
                      beat_ms=beat, applied_offset_ms=applied,
                      duration_s=len(y) / float(sr), tracks=track_reports,
                      warnings=warnings, removed_notes=removed_rows)
    pair._pm = pm
    pair._tracks = tmp_tracks

    # ---- 4. 输出 ----
    if emit:
        emit_pair(pair, cfg)
    return pair
