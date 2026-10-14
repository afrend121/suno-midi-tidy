# -*- coding: utf-8 -*-
"""纯 Python Ed25519 —— 只用于「离线授权码」的签名与验签，零第三方依赖。

用途：商户端用私钥签发授权码（sign），客户端内嵌公钥验签（verify）。
私钥永远不会进入分发包，因此客户端即使被反编译也拿不到签发能力。

实现基于 djb 的 Ed25519 参考实现（公有领域）与 Python `ed25519` 包的
公开算法，做了两点工程化处理：
  * scalarmult / expmod 改为**迭代**实现，避免 256 层递归踩到解释器上限；
  * 只暴露 sign / verify / keypair 三个入口，其余为内部函数。

性能：单次验签约 0.3–1 s（纯 Python 大整数运算），授权校验只在启动时做一次
并缓存，不影响主流程。
"""

from __future__ import annotations

import hashlib
import os
from typing import Tuple

__all__ = ["keypair", "publickey", "sign", "verify", "b64u_encode", "b64u_decode"]

b = 256
q = 2 ** 255 - 19
l = 2 ** 252 + 27742317777372353535851937790883648493


# --------------------------------------------------------------------------
# 底层数学
# --------------------------------------------------------------------------

def _sha512(m: bytes) -> bytes:
    return hashlib.sha512(m).digest()


def _expmod(base: int, exp: int, mod: int) -> int:
    """迭代快速幂。"""
    result = 1
    base %= mod
    while exp > 0:
        if exp & 1:
            result = result * base % mod
        base = base * base % mod
        exp >>= 1
    return result


def _inv(x: int) -> int:
    """模逆。用扩展欧几里得（3.8+ 的 pow(x,-1,q)），比费马小定理快一个数量级。"""
    try:
        return pow(x, -1, q)
    except (TypeError, ValueError):         # 老解释器 / 不可逆
        return _expmod(x, q - 2, q)


_d = -121665 * _inv(121666) % q
_I = _expmod(2, (q - 1) // 4, q)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * _inv(_d * y * y + 1) % q
    x = _expmod(xx, (q + 3) // 8, q)
    if (x * x - xx) % q != 0:
        x = x * _I % q
    if x % 2 != 0:
        x = q - x
    return x


_By = 4 * _inv(5) % q
_Bx = _xrecover(_By)
_B = [_Bx % q, _By % q]


def _edwards(p, q_):  # noqa: D401
    x1, y1 = p
    x2, y2 = q_
    k = _d * x1 * x2 * y1 * y2 % q
    x3 = (x1 * y2 + x2 * y1) * _inv(1 + k) % q
    y3 = (y1 * y2 + x1 * x2) * _inv(1 - k) % q
    return [x3 % q, y3 % q]


def _scalarmult(p, e: int):
    """迭代 double-and-add，返回 [x, y]。"""
    result = [0, 1]                      # 单位元
    base = list(p)
    while e > 0:
        if e & 1:
            result = _edwards(result, base)
        base = _edwards(base, base)
        e >>= 1
    return result


def _encodepoint(p) -> bytes:
    x, y = p
    bits = [(y >> i) & 1 for i in range(b - 1)] + [x & 1]
    return bytes(sum(bits[i * 8 + j] << j for j in range(8)) for i in range(b // 8))


def _decodepoint(s: bytes):
    y = int.from_bytes(s, "little") & ((1 << 255) - 1)
    x = _xrecover(y)
    if (x & 1) != _bit(s, 255):
        x = q - x
    p = [x, y]
    if not _isoncurve(p):
        raise ValueError("point not on curve")
    return p


def _isoncurve(p) -> bool:
    x, y = p
    return (-x * x + y * y - 1 - _d * x * x * y * y) % q == 0


def _bit(h: bytes, i: int) -> int:
    return (h[i // 8] >> (i % 8)) & 1


def _hint(m: bytes) -> int:
    h = _sha512(m)
    return sum(2 ** i * _bit(h, i) for i in range(2 * b))


def _clamp(h: bytes) -> int:
    return 2 ** (b - 2) + sum(2 ** i * _bit(h, i) for i in range(3, b - 2))


# --------------------------------------------------------------------------
# 公开接口
# --------------------------------------------------------------------------

def publickey(sk: bytes) -> bytes:
    """32 字节种子 -> 32 字节公钥。"""
    if len(sk) != 32:
        raise ValueError("私钥种子必须是 32 字节")
    return _encodepoint(_scalarmult(_B, _clamp(_sha512(sk))))


def sign(msg: bytes, sk: bytes) -> bytes:
    """用 32 字节种子签名，返回 64 字节签名。"""
    if len(sk) != 32:
        raise ValueError("私钥种子必须是 32 字节")
    h = _sha512(sk)
    a = _clamp(h)
    pk = _encodepoint(_scalarmult(_B, a))
    r = _hint(h[b // 8:b // 4] + msg)
    rp = _encodepoint(_scalarmult(_B, r))
    s = (r + _hint(rp + pk + msg) * a) % l
    return rp + s.to_bytes(32, "little")


def verify(sig: bytes, msg: bytes, pk: bytes) -> bool:
    """验签：签名 64B + 消息 + 公钥 32B。任何异常都返回 False。"""
    try:
        if len(sig) != 64 or len(pk) != 32:
            return False
        rp = _decodepoint(sig[:32])
        ap = _decodepoint(pk)
        s = int.from_bytes(sig[32:], "little")
        if s >= l:
            return False
        h = _hint(sig[:32] + pk + msg)
        return _scalarmult(_B, s) == _edwards(rp, _scalarmult(ap, h))
    except Exception:  # noqa: BLE001 —— 验签失败一律 False，不向上抛
        return False


def keypair() -> Tuple[bytes, bytes]:
    """生成 (私钥种子, 公钥)，各 32 字节。"""
    sk = os.urandom(32)
    return sk, publickey(sk)


# --------------------------------------------------------------------------
# URL 安全 Base64（去掉填充，方便塞进一行授权码）
# --------------------------------------------------------------------------

def b64u_encode(data: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64u_decode(text: str) -> bytes:
    import base64
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)
