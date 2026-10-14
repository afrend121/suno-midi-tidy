# -*- coding: utf-8 -*-
"""能力集定义 + 离线授权校验（本技能现已全功能免费开放）。

设计要点
--------
* **一份代码，全部能力。** 自 2026-10 起取消「免费版 / 专业版」分层，所有能力
  默认开放。能力矩阵仍保留在 `ALL_CAPS`，作为引擎唯一的「功能清单」。
* **门禁代码保留但不生效。** `apply_caps()` 依旧把配置钳制到 `caps` 集合，只是
  现在 `FREE_CAPS == ALL_CAPS`，钳制结果恒为空。将来若要重新分层，只需把
  `FREE_BASE` 改回某个子集，GUI / CLI / 引擎会自动跟着收敛，无需改调用方。
* **离线校验，不联网。** 历史授权码用 Ed25519 签名，客户端只内嵌公钥。验签
  模块保留：老用户的 `license.key` 仍能验通。当前它不影响任何功能的放行。

授权码格式（历史，保留兼容）
--------------------------
    SMT1.<base64url(payload_json)>.<base64url(64B 签名)>

payload 字段：v / p(产品) / ed(版本) / sub(客户) / iat(签发) / exp(到期,可空)
             / feat(能力,["*"] 表示全部) / mch(机器码,空=不限)

找码顺序：--license 参数 > 环境变量 SMT_LICENSE > 技能根目录 license.key >
          当前目录 license.key > ~/.suno-midi-tidy/license.key
"""

from __future__ import annotations

import datetime as _dt
import functools
import hashlib
import json
import os
import platform
import uuid
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Tuple

# --- 同目录导入（脚本方式运行时 sys.path 已含本目录，包方式运行时走相对导入） ---
try:                                              # pragma: no cover
    from . import _ed25519                        # type: ignore
except Exception:                                 # pragma: no cover
    import _ed25519                               # type: ignore

try:
    from _pubkey import PUBLIC_KEY_HEX            # type: ignore
except Exception:                                 # pragma: no cover
    try:
        from ._pubkey import PUBLIC_KEY_HEX       # type: ignore
    except Exception:
        PUBLIC_KEY_HEX = ""

PRODUCT = "suno-midi-tidy"
TOKEN_PREFIX = "SMT1"

# --------------------------------------------------------------------------
# 能力矩阵 —— 引擎唯一的功能清单（当前全部在免费版开放）
# --------------------------------------------------------------------------

CAP_ALIGN_APPLY = "align.apply"        # 真正改写时间轴对齐起音
CAP_SCORE_OUTPUT = "output.score"      # 量化版输出（MuseScore 出谱）
CAP_DUP_PRUNE = "clean.duplicate"      # 重复音清理
CAP_VELOCITY_BASIC = "velocity.basic"  # 基础力度重建
CAP_SUMMARY_REPORT = "report.summary"  # 汇总报告

CAP_MIX_OUTPUT = "output.mix"          # 演奏细节版输出（DAW 继续编曲）
CAP_TIMESIG = "output.timesig"         # 拍号写入 MIDI 元事件
CAP_SILENT_PRUNE = "clean.silent"      # 无声音符清理（只删 WAV 里确实没响的）
CAP_VOCAL_VERIFY = "clean.vocal"       # 人声音高证据核验（去串音/假八度）
CAP_FINE_VELOCITY = "velocity.fine"    # 24dB 动态范围的精细力度
CAP_BATCH = "batch.multi"              # 一次处理多个配对
CAP_FULL_REPORT = "report.full"        # 逐音删除清单（可回听审计）
CAP_LONG_AUDIO = "audio.long"          # 不受单次时长限制

ALL_CAPS: FrozenSet[str] = frozenset([
    CAP_ALIGN_APPLY, CAP_SCORE_OUTPUT, CAP_DUP_PRUNE,
    CAP_VELOCITY_BASIC, CAP_SUMMARY_REPORT,
    CAP_MIX_OUTPUT, CAP_TIMESIG, CAP_SILENT_PRUNE, CAP_VOCAL_VERIFY,
    CAP_FINE_VELOCITY, CAP_BATCH, CAP_FULL_REPORT, CAP_LONG_AUDIO,
])

# 免费版 = 全功能版：不分层，不锁任何能力。
# （若要恢复分层，把 FREE_BASE 改成 ALL_CAPS 的某个真子集即可。）
FREE_BASE: FrozenSet[str] = ALL_CAPS
PRO_ONLY: FrozenSet[str] = frozenset()
FREE_CAPS: FrozenSet[str] = FREE_BASE
PRO_CAPS: FrozenSet[str] = ALL_CAPS

CAP_LABELS: Dict[str, str] = {
    CAP_ALIGN_APPLY: "起音自动对齐",
    CAP_SCORE_OUTPUT: "量化版输出（出谱）",
    CAP_DUP_PRUNE: "重复音清理",
    CAP_VELOCITY_BASIC: "力度重建（基础）",
    CAP_SUMMARY_REPORT: "汇总报告",
    CAP_MIX_OUTPUT: "mix 演奏细节版输出（DAW）",
    CAP_TIMESIG: "拍号写入（3/4 等）",
    CAP_SILENT_PRUNE: "无声音符清理",
    CAP_VOCAL_VERIFY: "人声音高核验",
    CAP_FINE_VELOCITY: "精细力度（107 档）",
    CAP_BATCH: "批量处理（多文件）",
    CAP_FULL_REPORT: "逐音删除清单",
    CAP_LONG_AUDIO: "长音频（>3 分钟）",
}

# 历史分层时期的免费版硬限制。现已全部不生效（FREE_CAPS == ALL_CAPS）；
# 常量保留，供引擎的粗力度分支、以及 self-test 引用。
FREE_MAX_FILES = 1
FREE_MAX_DURATION_S = 180.0
FREE_VELOCITY_LEVELS = 8
FREE_VELOCITY_LADDER = (24, 40, 56, 72, 88, 104, 120, 127)


# --------------------------------------------------------------------------
# 机器码
# --------------------------------------------------------------------------

def machine_id() -> str:
    """稳定机器码：主机名 + 架构 + MAC。用于可选的一机一码绑定。"""
    raw = "|".join([platform.node(), platform.machine(), str(uuid.getnode())])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16].upper()


# --------------------------------------------------------------------------
# 授权码解析与校验
# --------------------------------------------------------------------------

@dataclass
class EditionInfo:
    edition: str = "free"                 # free | pro
    caps: FrozenSet[str] = FREE_CAPS
    licensed: bool = False
    status: str = "free"                  # free | valid | expired | invalid | machine-mismatch | missing
    message: str = ""
    holder: str = ""
    issued: str = ""
    expires: Optional[str] = None
    machine: str = ""
    source: str = ""
    days_left: Optional[int] = None

    @property
    def is_pro(self) -> bool:
        return self.edition == "pro"

    @property
    def valid(self) -> bool:
        """授权码本身是否验签通过且在有效期内（不含版本声明）。"""
        return self.licensed

    @property
    def locked(self) -> List[str]:
        return [CAP_LABELS[c] for c in sorted(ALL_CAPS - self.caps, key=lambda k: CAP_LABELS[k])]


@functools.lru_cache(maxsize=8)
def _verify_cached(token: str, check_machine: bool) -> EditionInfo:
    info = EditionInfo(source="")
    parts = token.strip().split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        info.status = "invalid"
        info.message = "授权码格式不正确（应为 SMT1.<payload>.<signature>）"
        return info
    _, payload_b64, sig_b64 = parts
    try:
        payload_bytes = _ed25519.b64u_decode(payload_b64)
        sig = _ed25519.b64u_decode(sig_b64)
    except Exception:
        info.status = "invalid"
        info.message = "授权码包含无法解码的字符"
        return info

    if not PUBLIC_KEY_HEX:
        info.status = "invalid"
        info.message = "客户端未内嵌验签公钥（请先运行 密钥生成工具 重新打包）"
        return info

    try:
        pk = bytes.fromhex(PUBLIC_KEY_HEX)
    except ValueError:
        info.status = "invalid"
        info.message = "内嵌公钥格式错误"
        return info

    if not _ed25519.verify(sig, payload_bytes, pk):
        info.status = "invalid"
        info.message = "签名校验未通过（授权码无效或被篡改）"
        return info

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        info.status = "invalid"
        info.message = "授权码载荷无法解析"
        return info

    if payload.get("p") != PRODUCT:
        info.status = "invalid"
        info.message = "授权码属于其他产品（%s）" % payload.get("p")
        return info
    if payload.get("ed") != "pro":
        info.status = "invalid"
        info.message = "授权码版本不受支持"
        return info

    info.holder = str(payload.get("sub") or "")
    info.issued = str(payload.get("iat") or "")
    info.expires = payload.get("exp") or None
    info.machine = str(payload.get("mch") or "")
    info.source = ""

    exp = info.expires
    if exp:
        try:
            exp_date = _dt.date.fromisoformat(str(exp))
        except ValueError:
            info.status = "invalid"
            info.message = "授权码到期日格式错误"
            return info
        today = _dt.date.today()
        info.days_left = (exp_date - today).days
        if info.days_left < 0:
            info.status = "expired"
            info.message = "授权码已于 %s 过期" % exp_date.isoformat()
            return info

    if check_machine and info.machine:
        if info.machine.upper() != machine_id():
            info.status = "machine-mismatch"
            info.message = ("授权码绑定机器 %s，本机为 %s"
                            % (info.machine, machine_id()))
            return info

    info.licensed = True
    info.edition = "pro"
    info.caps = PRO_CAPS
    info.status = "valid"
    if info.days_left is not None:
        info.message = "专业版已激活，剩余 %d 天" % info.days_left
    else:
        info.message = "专业版已激活（永久授权）"
    return info


def verify_token(token: str, check_machine: bool = True) -> EditionInfo:
    """校验授权码字符串，返回 EditionInfo（不改 edition.json 的声明）。"""
    return _verify_cached(token.strip(), bool(check_machine))


# --------------------------------------------------------------------------
# edition.json / 授权文件发现
# --------------------------------------------------------------------------

def skill_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_edition_config(root: Optional[str] = None) -> Dict[str, object]:
    root = root or skill_root()
    path = os.path.join(root, "edition.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def license_search_paths(root: Optional[str] = None) -> List[str]:
    root = root or skill_root()
    home = os.path.expanduser("~")
    return [
        os.path.join(root, "license.key"),
        os.path.join(os.getcwd(), "license.key"),
        os.path.join(home, ".suno-midi-tidy", "license.key"),
    ]


def find_license(explicit: Optional[str] = None,
                 root: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """返回 (来源路径, 授权码文本)；找不到则 ("", None)。"""
    env = os.environ.get("SMT_LICENSE")
    candidates: List[str] = []
    if explicit:
        candidates.append(explicit)
    if env:
        candidates.append(env)
    candidates.extend(license_search_paths(root))
    for path in candidates:
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read().strip()
                if text:
                    return path, text
            except OSError:
                continue
    return "", None


# --------------------------------------------------------------------------
# 主入口：解析当前生效的版本
# --------------------------------------------------------------------------

def resolve(license_path: Optional[str] = None,
            force_edition: Optional[str] = None,
            root: Optional[str] = None,
            check_machine: bool = True) -> EditionInfo:
    """综合 edition.json 声明 + 授权码，得出当前实际生效的能力集。"""
    root = root or skill_root()
    conf = read_edition_config(root)
    declared = (force_edition
                or os.environ.get("SMT_EDITION")
                or str(conf.get("edition") or "free")).strip().lower()
    if declared not in ("free", "pro"):
        declared = "free"

    src, token = find_license(license_path, root)
    if token:
        info = verify_token(token, check_machine=check_machine)
        info.source = src
        if info.valid:
            return info                      # 有效授权码 -> 记作专业版（历史兼容）
        # 有码但无效：按全功能免费版处理，并把原因带出去
        info.edition = "free"
        info.caps = FREE_CAPS
        info.licensed = False
        return info

    info = EditionInfo(edition="free", caps=FREE_CAPS, status="free")
    if declared == "pro":
        # 历史遗留：包曾声明 pro 但没带授权码。能力已全开放，不影响功能。
        info.status = "missing"
        info.message = "未找到授权码；全功能已开放，不影响使用"
    else:
        info.message = "全功能免费版（所有能力已开放，无需授权码）"
    return info


# --------------------------------------------------------------------------
# 能力门禁：把配置钳制到当前能力集
# --------------------------------------------------------------------------

def apply_caps(cfg, caps: FrozenSet[str], info: Optional[EditionInfo] = None) -> List[str]:
    """就地钳制 TidyConfig，返回被锁功能的说明列表（供 CLI 打印）。

    `caps` 为 None 表示不限制（引擎自测、内部调用）。
    """
    if caps is None:
        return []
    notes: List[str] = []

    if CAP_ALIGN_APPLY not in caps and getattr(cfg, "apply_align", False):
        cfg.apply_align = False
        cfg.align_mode = "none"
        notes.append("起音对齐（应用）")

    if CAP_MIX_OUTPUT not in caps and getattr(cfg, "emit_mix", False):
        cfg.emit_mix = False
        notes.append("mix 演奏细节版输出")

    if CAP_TIMESIG not in caps and getattr(cfg, "time_sig", None):
        cfg.time_sig = None
        notes.append("拍号写入")

    if CAP_SILENT_PRUNE not in caps and getattr(cfg, "delete_silent", False):
        cfg.delete_silent = False
        notes.append("无声音符清理")

    if CAP_VOCAL_VERIFY not in caps and getattr(cfg, "vocal_check", "off") != "off":
        cfg.vocal_check = "off"
        notes.append("人声音高核验")

    if CAP_FINE_VELOCITY not in caps:
        cfg.coarse_velocity = True
        notes.append("精细力度（限 %d 档）" % FREE_VELOCITY_LEVELS)

    if CAP_FULL_REPORT not in caps:
        cfg.full_report = False
        notes.append("逐音删除清单")

    if CAP_LONG_AUDIO not in caps:
        cfg.audio_limit_s = FREE_MAX_DURATION_S
        notes.append("长音频（单次 ≤ %.0f 秒）" % FREE_MAX_DURATION_S)

    if CAP_BATCH not in caps:
        notes.append("批量处理（单次 %d 个配对）" % FREE_MAX_FILES)

    return notes


def coarsen_velocity(v: int) -> int:
    """把精细力度压到粗阶梯上（≤ FREE_VELOCITY_LEVELS 档）。"""
    return min(FREE_VELOCITY_LADDER, key=lambda x: abs(x - int(v)))


def edition_banner(info: EditionInfo) -> str:
    tag = "专业版 PRO" if info.is_pro else "全功能免费版"
    line = "=" * 62
    out = [line, "  Suno MIDI 整音  ·  %s" % tag, line]
    if info.is_pro:
        holder = ("  授权给 %s" % info.holder) if info.holder else ""
        out.append("  %s%s" % (info.message, ("  |" + holder if holder else "")))
    else:
        out.append("  %s" % info.message)
        out.append("  已开放：起音对齐 / 力度重建 / 清理（重复·哑音·人声）"
                   " / mix+score 双输出")
        out.append("  批量处理与长音频无限制；无需授权码，全程离线。")
    out.append(line)
    return "\n".join(out)


class EditionLimit(RuntimeError):
    """触碰到时长上限时抛出（当前上限为 None，不会触发）。"""


def check_duration(duration_s: float, cfg) -> None:
    limit = getattr(cfg, "audio_limit_s", None)
    if limit and duration_s > limit + 0.5:
        raise EditionLimit(
            "音频时长 %.1f 秒超过上限 %.0f 秒"
            % (duration_s, limit))
