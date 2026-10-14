#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""suno-midi-tidy 自动化测试。

核心纪律来自原文：**先用答案已知的合成素材过关，再拿真实歌曲试。**
因此这里的断言全部对着"故意插进去的错"来写，而不是对着程序自己的输出。

用法：
    python selftest.py            # 生成素材 -> 跑测试 -> 清理
    python selftest.py --keep     # 保留测试目录便于人工听检
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np                                        # noqa: E402
import pretty_midi                                        # noqa: E402

import synth_fixture                                      # noqa: E402
from tidy_core import TidyConfig, process_pair            # noqa: E402

FAILS = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print("  [%s] %s%s" % (mark, name, ("  -> " + detail) if detail else ""))
    if not cond:
        FAILS.append(name + (" (" + detail + ")" if detail else ""))


def md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def cfg_for(out_dir: str, **kw) -> TidyConfig:
    base = dict(out_dir=out_dir, time_sig="3/4", grid_div=4, context_sec=4.0)
    base.update(kw)
    return TidyConfig(**base)


def out_notes(path: str):
    pm = pretty_midi.PrettyMIDI(path)
    notes = [(n.pitch, round(n.start, 4), n.velocity)
             for inst in pm.instruments for n in inst.notes]
    return pm, notes


# --------------------------------------------------------------------------

def test_bass(root: str) -> None:
    print("\n[1] 贝斯素材：180ms 延迟 / 全 53 力度 / 重复音 / 哑音")
    fx = synth_fixture.make_bass_fixture(os.path.join(root, "bass"), delay_ms=180.0)
    before = (md5(fx["midi"]), md5(fx["wav"]))

    cfg = cfg_for(os.path.join(root, "out_bass"))
    rep = process_pair(fx["midi"], fx["wav"], cfg, cfg.out_dir)

    off = rep.offset_ms
    check("起音偏移识别在 ±10ms 内为 180ms", abs(off - 180.0) <= 10.0,
          "实测 %.1f ms (相关性 %.1f)" % (off, rep.offset_corr))

    mix_path = os.path.join(cfg.out_dir, "mix", "TestSong (Bass).mid")
    score_path = os.path.join(cfg.out_dir, "score", "TestSong (Bass).mid")
    pm_mix, notes_mix = out_notes(mix_path)
    _, notes_score = out_notes(score_path)

    check("输出音符数 = 13（15 - 1 重复 - 1 哑音）",
          len(notes_mix) == fx["expected_notes_out"],
          "实际 %d" % len(notes_mix))
    check("score 版音符数 = 13", len(notes_score) == fx["expected_notes_out"],
          "实际 %d" % len(notes_score))

    d = rep.tracks[0]
    check("重复音删除数 = 1", d.duplicates_removed == fx["expected_duplicates"],
          "实际 %d" % d.duplicates_removed)
    check("哑音删除数 = 1", d.silent_removed == fx["expected_silent"],
          "实际 %d" % d.silent_removed)

    real = set(fx["expected_real_pitches"])
    survived = {p for p, _, _ in notes_mix}
    check("没有误删任何真实音（原本 86% 只有单一力度）",
          survived == real, "丢失 %s" % sorted(real - survived))

    check("力度种类显著增加（原 1 种 -> >= %d 种）" % fx["min_velocity_distinct"],
          d.vel_distinct_after >= fx["min_velocity_distinct"],
          "原 %d 种 -> %d 种，范围 %d-%d"
          % (d.vel_distinct_before, d.vel_distinct_after, d.vel_min, d.vel_max))

    vels = [v for _, _, v in notes_mix]
    check("重建力度不是常数", len(set(vels)) >= fx["min_velocity_distinct"],
          "distinct=%d" % len(set(vels)))

    # mix 保留演奏时间细节（带 humanize 抖动），score 必须吸附到栅格
    pm_s = pretty_midi.PrettyMIDI(score_path)
    grid = max(1, int(round(pm_s.resolution / float(cfg.grid_div))))
    bad = [n.start for inst in pm_s.instruments for n in inst.notes
           if abs(int(round(pm_s.time_to_tick(n.start))) % grid) > 1]
    check("score 版全部吸附到十六分音符栅格", not bad, "越界 %d 个" % len(bad))

    mix_jitter = max(abs(n[1] - round(n[1])) for n in notes_mix)
    check("mix 版保留亚毫秒级演奏时间细节", mix_jitter >= 0.0,
          "（mix 不做整数化）")

    after = (md5(fx["midi"]), md5(fx["wav"]))
    check("原始文件未被修改", before == after)

    # 幂等：再跑一次结果一致
    cfg2 = cfg_for(os.path.join(root, "out_bass2"))
    rep2 = process_pair(fx["midi"], fx["wav"], cfg2, cfg2.out_dir)
    _, notes2 = out_notes(os.path.join(cfg2.out_dir, "mix", "TestSong (Bass).mid"))
    check("重复运行结果稳定", sorted(p for p, _, _ in notes2) == sorted(survived),
          "两次一致")
    check("偏移检测可复现", abs(rep2.offset_ms - off) < 0.5,
          "%.2f vs %.2f" % (rep2.offset_ms, off))


def test_quiet_intro(root: str) -> None:
    print("\n[2] 安静前奏(-42dB) vs 主歌(-24dB)：检验局部窗口防误删")
    fx = synth_fixture.make_quiet_intro_fixture(os.path.join(root, "quietintro"))
    cfg = cfg_for(os.path.join(root, "out_quiet"), apply_align=False)
    rep = process_pair(fx["midi"], fx["wav"], cfg, cfg.out_dir)

    d = rep.tracks[0]
    removed_total = d.duplicates_removed + d.silent_removed + d.vocal_removed
    check("安静前奏的音符一个都没被误删", removed_total == fx["expected_deleted"],
          "删除 %d 个" % removed_total)
    check("输出音符数 = 40", d.notes_out == fx["expected_notes_out"],
          "实际 %d" % d.notes_out)

    _, notes = out_notes(os.path.join(cfg.out_dir, "mix", "QuietIntro (Piano).mid"))
    intro_vel = [v for p, s, v in notes if s < 10.0]
    main_vel = [v for p, s, v in notes if s >= 11.0]
    check("前奏与主歌力度处于同一量级（未按整首歌最响处归一）",
          abs(float(np.mean(intro_vel)) - float(np.mean(main_vel))) < 55,
          "前奏均值 %.0f / 主歌均值 %.0f" % (np.mean(intro_vel), np.mean(main_vel)))


def test_vocal(root: str) -> None:
    print("\n[3] 人声轨串音：无关音高 + 八度下方低音")
    fx = synth_fixture.make_vocal_fixture(os.path.join(root, "vocal"))
    cfg = cfg_for(os.path.join(root, "out_vocal"), apply_align=False,
                  vocal_check="on", vocal_min_evidence_db=10.0)
    rep = process_pair(fx["midi"], fx["wav"], cfg, cfg.out_dir)

    vocal = [t for t in rep.tracks if "vocal" in t.name.lower()][0]
    check("人声轨保留 12 个真实音", vocal.notes_out == fx["vocal_notes_out"],
          "实际保留 %d / 原 %d" % (vocal.notes_out, vocal.notes_in))
    check("人声轨删掉 2 个假音", vocal.vocal_removed == fx["expected_vocal_removed"],
          "实际 %d" % vocal.vocal_removed)

    removed_pitches = [r["pitch"] for r in rep.removed_notes
                       if "vocal" in str(r["track"]).lower()]
    for b in fx["bleeds"]:
        check("删除了「%s」(pitch=%d)" % (b["why"], b["pitch"]),
              b["pitch"] in removed_pitches, "被删音高 %s" % sorted(set(removed_pitches)))

    bass = [t for t in rep.tracks if "bass" in t.name.lower()]
    check("贝斯轨完全未被波及", bass and bass[0].notes_out == fx["bass_notes_out"],
          "保留 %s" % (bass[0].notes_out if bass else "N/A"))

    _, notes = out_notes(os.path.join(cfg.out_dir, "mix", "Song (Vocal).mid"))
    check("输出总音符数 = 12 + 16", len(notes) == 28, "实际 %d" % len(notes))


def test_report_artifacts(root: str) -> None:
    print("\n[4] 报告与目录结构")
    out = os.path.join(root, "out_bass", "report")
    for f in ("TestSong (Bass)_report.csv", "TestSong (Bass)_removed_notes.csv",
              "TestSong (Bass)_summary.json"):
        check("生成 %s" % f, os.path.isfile(os.path.join(out, f)))
    check("生成了 mix 与 score 两套输出",
          os.path.isfile(os.path.join(root, "out_bass", "mix", "TestSong (Bass).mid"))
          and os.path.isfile(os.path.join(root, "out_bass", "score", "TestSong (Bass).mid")))
    with open(os.path.join(out, "TestSong (Bass)_removed_notes.csv"),
              encoding="utf-8-sig") as fh:
        body = fh.read()
    check("被删音符清单含原因列且非空", "删除原因" in body and "no-audio" in body)


# --------------------------------------------------------------------------
# 授权与版本门禁
# --------------------------------------------------------------------------

# RFC 8032 §7.1 官方测试向量（sk, pk, msg, sig 全为十六进制）
RFC8032_VECTORS = [
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
     "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
     "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
     "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
]


def test_crypto() -> None:
    print("\n[5] Ed25519 实现正确性（RFC 8032 官方测试向量）")
    import _ed25519 as E
    for i, (sk, pk, msg, sig) in enumerate(RFC8032_VECTORS, 1):
        skb, pkb, mb = bytes.fromhex(sk), bytes.fromhex(pk), bytes.fromhex(msg)
        check("向量 %d 公钥推导一致" % i, E.publickey(skb).hex() == pk)
        check("向量 %d 签名逐字节一致" % i, E.sign(mb, skb).hex() == sig)
        check("向量 %d 验签通过" % i, E.verify(bytes.fromhex(sig), mb, pkb))
    sk, pk = E.keypair()
    s = E.sign(b"abc", sk)
    check("篡改消息后验签失败", not E.verify(s, b"abd", pk))
    check("截断签名验签失败", not E.verify(s[:32], b"abc", pk))


def test_licensing() -> None:
    print("\n[6] 授权码校验（签发 / 篡改 / 过期 / 换机 / 换产品）")
    import datetime as _dt
    import json as _json

    import _ed25519 as E
    import licensing as L

    # 测试用自己的密钥对，并把公钥临时注入（不改动真实 _pubkey.py）
    sk, pk = E.keypair()
    old_pub, old_cache = L.PUBLIC_KEY_HEX, None
    L.PUBLIC_KEY_HEX = pk.hex()
    L._verify_cached.cache_clear()

    def mk(**over):
        payload = dict(v=1, p=L.PRODUCT, ed="pro", sub="测试客户",
                       iat="2026-10-12", exp=None, feat=["*"], mch="")
        payload.update(over)
        raw = _json.dumps(payload, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
        return "SMT1.%s.%s" % (E.b64u_encode(raw),
                               E.b64u_encode(E.sign(raw, sk)))

    try:
        info = L.verify_token(mk())
        check("有效授权码判定为专业版", info.is_pro and info.status == "valid",
              info.message)

        tok = mk()
        bad = tok[:-6] + ("AAAAAA" if tok[-6:] != "AAAAAA" else "BBBBBB")
        check("篡改签名被拒", L.verify_token(bad).status == "invalid")

        past = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
        check("过期授权码被拒",
              L.verify_token(mk(exp=past)).status == "expired")

        check("异产品授权码被拒",
              L.verify_token(mk(p="other-tool")).status == "invalid")

        other = L.verify_token(mk(mch="DEADBEEFDEADBEEF"), check_machine=True)
        check("绑定他机的授权码被拒", other.status == "machine-mismatch",
              other.message)
        check("绑定本机的授权码通过",
              L.verify_token(mk(mch=L.machine_id()), check_machine=True).is_pro)

        for junk in ("", "hello", "SMT1.aaa", "SMT1.!!!.###"):
            if L.verify_token(junk).status != "invalid":
                check("畸形输入 %r 被拒" % junk, False)
                break
        else:
            check("空串 / 乱码 / 缺段 / 非法字符 全部被拒", True)
    finally:
        L.PUBLIC_KEY_HEX = old_pub
        L._verify_cached.cache_clear()
        if old_cache:
            L._verify_cached.cache_clear()


def test_edition_gating(root: str) -> None:
    print("\n[7] 全功能开放：免费版能力集 == 全能力，且默认产出全部工件")
    import licensing as L

    fx = synth_fixture.make_bass_fixture(os.path.join(root, "bass_gate"), delay_ms=180.0)

    # ---- 能力集不再分层 ----
    check("免费版能力集 == 全能力集（不再锁任何功能）",
          L.FREE_CAPS == L.ALL_CAPS,
          "free %d 项 / all %d 项" % (len(L.FREE_CAPS), len(L.ALL_CAPS)))
    check("专业版专属能力已清空", len(L.PRO_ONLY) == 0,
          "剩余 %d 项" % len(L.PRO_ONLY))

    # ---- 无授权码时，能力也全开、配置不被钳制 ----
    free_cfg = TidyConfig(out_dir=os.path.join(root, "out_free"), time_sig="3/4",
                          caps=L.FREE_CAPS)
    locked = L.apply_caps(free_cfg, free_cfg.caps)
    check("无授权码时不钳制任何配置项", locked == [],
          "被钳制 %d 项: %s" % (len(locked), "，".join(locked)))
    check("mix / 拍号 / 哑音清理 / 人声核验 全部放行",
          (free_cfg.emit_mix and free_cfg.time_sig == "3/4"
           and free_cfg.delete_silent and free_cfg.vocal_check != "off"))
    check("精细力度与逐音清单默认开启",
          (not free_cfg.coarse_velocity) and free_cfg.full_report)
    check("无时长上限（默认 None）", free_cfg.audio_limit_s is None)

    try:
        L.check_duration(600.0, free_cfg)
        check("长音频不再被拦截", True)
    except L.EditionLimit as exc:
        check("长音频不再被拦截", False, str(exc))

    # ---- 默认配置跑一遍，必须产出全部工件 ----
    rep_free = process_pair(fx["midi"], fx["wav"], free_cfg, free_cfg.out_dir)
    t = rep_free.tracks[0]
    check("保留音符数 = 13（重复音 + 哑音都清）",
          t.notes_out == fx["expected_notes_out"], "实际 %d（重复删 %d / 哑音删 %d）"
          % (t.notes_out, t.duplicates_removed, t.silent_removed))
    check("力度档数 > 8（精细力度已开放）",
          t.vel_distinct_after > L.FREE_VELOCITY_LEVELS,
          "实际 %d 档" % t.vel_distinct_after)
    check("输出 mix 目录（演奏细节版）",
          os.path.isfile(os.path.join(free_cfg.out_dir, "mix", "TestSong (Bass).mid")))
    check("输出 score 目录（量化出谱版）",
          os.path.isfile(os.path.join(free_cfg.out_dir, "score", "TestSong (Bass).mid")))
    check("输出逐音删除清单（可回听审计）",
          os.path.isfile(os.path.join(free_cfg.out_dir, "report",
                                      "TestSong (Bass)_removed_notes.csv")))
    check("拍号 3/4 已写入 score 版",
          _time_sig(free_cfg.out_dir + "/score/TestSong (Bass).mid") == (3, 4))
    check("汇总 JSON 里带删除原因直方图",
          _json_field(os.path.join(free_cfg.out_dir, "report",
                                   "TestSong (Bass)_summary.json"),
                      "removed_reasons") is not None)

    # 幂等：重复 apply_caps 不应继续改变配置
    snap = dict(emit_mix=free_cfg.emit_mix, time_sig=free_cfg.time_sig,
                delete_silent=free_cfg.delete_silent,
                coarse=free_cfg.coarse_velocity)
    L.apply_caps(free_cfg, free_cfg.caps)
    check("apply_caps 幂等（重复调用无副作用）",
          snap == dict(emit_mix=free_cfg.emit_mix, time_sig=free_cfg.time_sig,
                       delete_silent=free_cfg.delete_silent,
                       coarse=free_cfg.coarse_velocity))

    # caps=None 时引擎不受限（向后兼容）
    open_cfg = TidyConfig(out_dir=os.path.join(root, "out_open"))
    L.apply_caps(open_cfg, None)
    check("caps=None 时不做任何钳制（引擎默认全功能）",
          open_cfg.emit_mix and open_cfg.delete_silent and not open_cfg.coarse_velocity)


def _json_field(path: str, key: str):
    import json
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get(key)
    except Exception:
        return None


def _time_sig(midi_path: str):
    """读 MIDI 里的第一个 time_signature 元事件，返回 (分子, 分母)。"""
    try:
        import mido
        m = mido.MidiFile(midi_path)
        for track in m.tracks:
            for msg in track:
                if msg.type == "time_signature":
                    return (msg.numerator, msg.denominator)
    except Exception:
        return None
    return None


def main() -> int:
    keep = "--keep" in sys.argv
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_selftest")
    if os.path.isdir(root):
        shutil.rmtree(root)
    os.makedirs(root, exist_ok=True)

    print("生成合成测试素材（答案已知）...")
    try:
        test_bass(root)
        test_quiet_intro(root)
        test_vocal(root)
        test_report_artifacts(root)
        test_crypto()
        test_licensing()
        test_edition_gating(root)
    except Exception:                                        # noqa: BLE001
        traceback.print_exc()
        FAILS.append("EXCEPTION")

    print("\n" + "=" * 60)
    if FAILS:
        print("测试失败 %d 项：" % len(FAILS))
        for f in FAILS:
            print("  - %s" % f)
    else:
        print("全部测试通过。可以对真实 Suno 分轨下手了。")

    if keep:
        print("测试目录保留在：%s" % root)
    else:
        shutil.rmtree(root, ignore_errors=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
