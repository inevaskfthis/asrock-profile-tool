#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""asrock_profile.py — ASRock BIOS 用户配置档案（U 盘档案）编辑器  [纯命令行]

在 ASRock 主板上，BIOS 提供 "Save User Default to USB flash drive" /
"Load User Default from USB flash drive"。导出的档案里嵌着一整份
UEFI Setup 变量的原样副本；载入档案时由固件自己把它写回活体变量。

本工具就是改这份副本里的指定字节 —— 用来修改那些「BIOS 菜单里没有条目、
但固件/驱动会读取」的设置（例如 VDDCR_SOC / CPU Load-Line Calibration）。

⚠️ 重要：本工具**不会**让 BIOS 菜单里多出条目。菜单里有没有某个选项由
       Setup 模块的 IFR 决定，跟本档案无关。改档只能改「值」，不能加「条目」。

怎么用
------
    asrock_profile info  <档案>       看档案信息和当前值
    asrock_profile get  <档案> <字段>  读单个字段
    asrock_profile set  <档案> --llc 3 改字段并写出新档案
    asrock_profile detect <镜像>       看当前生效的 CPU LLC 是几级
    asrock_profile -h                 完整帮助

把档案直接拖到 exe 上也可以 —— 等价于 `asrock_profile info <档案>`。

License: MIT
Author:  inevaskfthis
"""

from __future__ import annotations

import argparse
import builtins
import contextlib
import io
import os
import re
import struct
import subprocess
import sys
from collections import Counter

__version__ = "1.4.0"
__author__ = "inevaskfthis"

# 若被以 --noconsole 方式打包（sys.stdout 为 None），print 会崩；这里兜底。
if sys.stdout is None:                       # pragma: no cover
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:                       # pragma: no cover
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


# --------------------------------------------------------------------------- #
# 控制台编码兼容层
# --------------------------------------------------------------------------- #
#
# Windows 控制台默认是 cp936(GBK)：中文没问题，但下面这些符号不在 GBK 里 ——
#     ⚠ U+26A0 / ✔ U+2714 / ✘ U+2718 / ⇒ U+21D2 / ↔ U+2194（另有 U+FE0F 变体选择符）
# 直接 print 会抛 UnicodeEncodeError 把程序打挂。实测 PyInstaller 打出来的 exe
# 在 cmd 里跑 `info <不匹配板型的档案>`（走 warning 分支）**必崩**，所以这层是必需的。
#
# 做法是两层保险：
#   1) reconfigure(errors="replace")   —— 兜住任何漏网字符，保证永不崩；
#   2) 自定义 print()，逐字符判断能否用当前流的编码写出，写不出的换成 ASCII 替身
#      （✔→[OK]、✘→[X]、⚠→[!]、⇒→=>、↔→<->），这样在 GBK 控制台里也可读。
#
# 流编码为 None（io.StringIO，GUI 抓日志用的就是这个）或 utf-8（Git Bash / 管道 /
# 重定向到文件）时**原样输出**，不降级 —— 那些环境本来就支持 Unicode。

_ASCII_MAP = {
    "\u2714": "[OK]",     # ✔
    "\u2718": "[X]",      # ✘
    "\u26a0": "[!]",      # ⚠
    "\ufe0f": "",         # 变体选择符，直接丢掉
    "\u21d2": "=>",       # ⇒
    "\u2194": "<->",      # ↔
}


def _setup_stdio():
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(errors="replace")
        except Exception:
            pass


_setup_stdio()


def _sanitize(text, stream=None):
    """把当前流写不出的字符换成 ASCII 替身；能写就原样返回。"""
    stream = stream if stream is not None else sys.stdout
    enc = getattr(stream, "encoding", None)
    if not enc:                      # StringIO / 无编码信息 ⇒ 不降级
        return text
    out = []
    for ch in text:
        if ord(ch) < 128:
            out.append(ch)
            continue
        try:
            ch.encode(enc)
            out.append(ch)
        except Exception:
            out.append(_ASCII_MAP.get(ch, "?"))
    return "".join(out)


def print(*args, **kwargs):          # noqa: A001  —— 有意遮蔽内置 print
    """编码安全版 print（见上面「控制台编码兼容层」）。"""
    stream = kwargs.get("file") or sys.stdout
    builtins.print(*[_sanitize(a, stream) if isinstance(a, str) else a for a in args],
                   **kwargs)


def encoding_hint(stream=None):
    """终端编码显示不了中文时，给一条**纯 ASCII 英文**提示。

    为什么用英文：这条提示出现的场景正是"中文会变乱码"的终端（典型是
    Windows 英文/欧洲语系的 cp1252 控制台）—— 提示本身要是中文就没法读了。
    """
    if _LANG != LANG_ZH:
        return                       # 已经用英文输出了，没必要再提示中文的事
    stream = stream if stream is not None else sys.stderr
    enc = getattr(sys.stdout, "encoding", None)
    if not enc:
        return
    try:
        "\u4e2d\u6587".encode(enc)
        return                       # 能表示中文，不用提示
    except Exception:
        pass
    builtins.print(
        "[!] Terminal encoding is %s, which cannot render this tool's Chinese output "
        "(it will show as '?')." % enc,
        file=stream)
    builtins.print(
        "    Fix: on Windows run `chcp 65001`, or set PYTHONIOENCODING=utf-8 "
        "(any OS).", file=stream)


# --------------------------------------------------------------------------- #
# 国际化（i18n）
# --------------------------------------------------------------------------- #
#   约定：
#     · **中文是源语言** —— 代码里直接写中文，它就是 zh 模式下的显示文本；
#     · T(s) 在英文模式下查 EN 表（key 就是那句中文原文，gettext 风格），
#       查不到就原样返回 ⇒ 最坏情况只是"漏译成中文"，永远不会崩；
#     · EN 表在**文件末尾**（「i18n 译文表」一节），运行期才查，所以定义在后面没问题；
#     · 语言选取优先级：--lang / $ASR_LANG  >  终端能不能渲染中文  >  系统 locale。
#
#   ⚠ 新增文案时**别**再写模块级 / 类级常量（例如 `X = "中文"`）——
#     那会在**导入时**求值，语言一换就固化了。放进函数里，或者在使用点才 T(...)。

LANG_ZH = "zh"
LANG_EN = "en"
LANGS = (LANG_ZH, LANG_EN)

_LANG = LANG_ZH


def get_lang() -> str:
    """当前语言。"""
    return _LANG


def lang_name(code: str) -> str:
    """语言名 —— 各语言用**自己的**写法，不进译文表（换成中文/英文反而不认识）。"""
    return {LANG_ZH: "\u4e2d\u6587", LANG_EN: "English"}.get(code, code)


def normalize_lang(v):
    """把各种写法归一成 zh / en；认不出来返回 None。"""
    if not v:
        return None
    v = str(v).strip().lower().replace("_", "-")
    if v.startswith("zh") or v in ("cn", "chinese", "chinese-simplified"):
        return LANG_ZH
    if v.startswith("en") or v in ("english", "eng"):
        return LANG_EN
    return None


def stream_can_render_cjk(stream=None) -> bool:
    """当前流能不能写出中文 —— 这是「自动选语言」的技术底线。"""
    stream = stream if stream is not None else sys.stdout
    enc = getattr(stream, "encoding", None)
    if not enc:
        return True                      # 没有编码信息（StringIO 等）⇒ 不做限制
    try:
        "\u4e2d\u6587".encode(enc)
        return True
    except Exception:
        return False


def detect_lang() -> str:
    """没有显式指定语言时，按环境猜一个。"""
    # 1) 环境变量 —— 最明确的用户意图
    code = normalize_lang(os.environ.get("ASR_LANG"))
    if code:
        return code
    # 2) POSIX 的 locale
    #    ⚠ Windows 上**不看** LANG / LC_ALL：Git Bash / MSYS 会塞一个 en_US.UTF-8，
    #      那只是 Git 自带的默认值，不代表用户想用英文，照做会很意外。
    if sys.platform != "win32":
        for k in ("LC_ALL", "LC_MESSAGES", "LANG"):
            v = os.environ.get(k)
            if v and v not in ("C", "POSIX"):
                return LANG_ZH if v.lower().replace("_", "-").startswith("zh") else LANG_EN
    # 3) 终端根本写不出中文 ⇒ 只能英文（这正是过去那条英文提示的场景）
    if not stream_can_render_cjk():
        return LANG_EN
    # 4) 没有别的判据 ⇒ 中文（本工具的主要用户群）
    return LANG_ZH


def set_lang(code=None) -> str:
    """设置语言；code 为 None 或认不出来时走自动判断。返回生效值。"""
    global _LANG
    _LANG = normalize_lang(code) or detect_lang()
    return _LANG


def T(s):
    """取当前语言的文案：中文模式原样返回，英文模式查表。"""
    if _LANG == LANG_ZH or not isinstance(s, str):
        return s
    return EN.get(s, s)


def no_change_text() -> str:
    """下拉框里的「不改」哨兵**文本**。

    为什么不写成类属性：类体在**导入时**求值，语言一换就固化了。
    """
    return T(T("(不改)"))


def _extract_lang(argv):
    """粗扫 argv 里的 --lang 并**把它摘掉**，返回 (值, 新 argv)。

    为什么不交给 argparse 解析：argparse 的 help 文本是**建 parser 时**取译文的，
    等解析完再切语言，`-h` 就已经用错的语言渲染好了。
    摘掉还有第二个好处：`asrock_profile info x --lang en` 这种把选项写在子命令
    后面的写法也能用（否则子 parser 会报「未识别的参数」）。
    """
    out, val, i = [], None, 0
    while i < len(argv):
        a = argv[i]
        if a == "--lang" and i + 1 < len(argv):
            val, i = argv[i + 1], i + 2
            continue
        if a.startswith("--lang="):
            val, i = a.split("=", 1)[1], i + 1
            continue
        out.append(a)
        i += 1
    return val, out


# --------------------------------------------------------------------------- #
# 常量与已知字段表
# --------------------------------------------------------------------------- #

#: 档案头部两个定长 ASCII 字段的长度
HEADER_FIELD_LEN = 32

#: 探测 Setup 变量块时搜索的长度前缀区间（覆盖常见 AMI Setup 变量尺寸）
SETUP_SIZE_MIN = 0x100
SETUP_SIZE_MAX = 0x1000

#: 已知主板标识（board id → 人类可读名）
KNOWN_BOARDS = {
    "A1818": "ASRock X370 Killer SLI / X370 Gaming K4 (A1818)",
}

#: 已实测过的 BIOS 版本（board id → 版本字符串集合，空集合 = 不校验版本）。
#: 版本字符串保留原样（注意前导空格，如 " 10.50"）。
KNOWN_VERSIONS = {
    "A1818": {" 10.50"},
}

#: Setup 变量里已知的字段（基于 ASRock X370 Killer SLI / Gaming K4 10.50 逆向）
#: size 以字节计；kind 用于取值校验
KNOWN_FIELDS = [
    # offset, size, kind, name
    (0x18B, 1, "mode",  "CPU Vcore Voltage mode"),
    (0x18C, 4, "raw",   "CPU Vcore Fixed Voltage (mV)"),
    (0x190, 1, "llc",   "CPU Load-Line Calibration"),
    (0x191, 1, "llc",   "VDDCR_SOC Load-Line Calibration"),
    (0x194, 2, "raw",   "CPU Vcore Offset Voltage"),
    (0x196, 1, "mode",  "VDDCR_SOC Voltage mode"),
    (0x197, 4, "raw",   "VDDCR_SOC Fixed Voltage (mV)"),
    (0x19B, 2, "raw",   "VDDCR_SOC Offset Voltage"),
    (0x1A4, 2, "mv",    "VDDP (mV)"),
    (0x1A6, 2, "mv",    "VTT_DDR (mV)"),
    (0x1A8, 2, "mv",    "DRAM Voltage (mV)"),
    (0x1AA, 2, "mv",    "1.05V_PROM Voltage (mV)"),
    (0x1AC, 2, "mv",    "CPU VDD 1.8 Voltage (mV)"),
    (0x1AE, 2, "mv",    "2.50V_PROM Voltage (mV)"),
    (0x1B0, 1, "raw",   "VPPM"),
    (0x1B1, 1, "raw",   "VDDCR SOC Voltage"),
    (0x1B2, 1, "raw",   "Vcore Offset Voltage"),
    (0x233, 1, "mode",  "CPU Frequency and Voltage(VID) Change"),
    (0x262, 1, "mode",  "SoC/Uncore OC Mode"),
]

FIELD_BY_OFFSET = {off: (size, kind, name) for off, size, kind, name in KNOWN_FIELDS}

#: 别名 → Setup 变量偏移（CLI 友好名）
ALIASES = {
    "cpu-llc": 0x190,
    "cpullc": 0x190,
    "cpu-loadline": 0x190,
    "soc-llc": 0x191,
    "socllc": 0x191,
    "vddcr-soc-llc": 0x191,
    "vddcr-soc-voltage": 0x1B1,
    "vcore-offset": 0x1B2,
    "vddp": 0x1A4,
    "vtt-ddr": 0x1A6,
    "dram-voltage": 0x1A8,
    "vpmm": 0x1B0,
    "soc-uncore-oc-mode": 0x262,
}

#: Load-Line Calibration 取值表
LLC_OPTIONS = {
    0: "Auto",
    1: "Level 1",
    2: "Level 2",
    3: "Level 3",
    4: "Level 4",
    5: "Level 5",
}

#: 需要按 0..5 校验的偏移
LLC_OFFSETS = (0x190, 0x191)

def unknown_board_hint():
    """没收录的板型 / 可疑偏移表时给用户的提示。

    写成函数而不是模块级常量：模块级常量在导入时就求值了，语言一换不回来。
    """
    return (
        T("字段偏移表只对已实测的板型负责。请先用 `info` 核对几条已知量是否合理：\n"
        "    Setup+0x1A8 DRAM Voltage 应是合理内存电压（如 1200–1500 mV），\n"
        "    且 Setup+0x1A6 VTT_DDR 约为它的一半。\n"
        "    数值明显不合理 ⇒ **不要改**，先把 `info` 输出贴到 issue 补字段表。")
    )

# 输出着色（仅在 TTY 上启用）
_TTY = sys.stdout.isatty()


def set_color(enabled: bool):
    """全局开关 ANSI 着色（GUI 里关掉）。"""
    global _TTY
    _TTY = bool(enabled)


def _c(code: str, text: str) -> str:
    if not _TTY:
        return text
    return "\033[%sm%s\033[0m" % (code, text)


def red(t: str) -> str:
    return _c("31", t)


def green(t: str) -> str:
    return _c("32", t)


def yellow(t: str) -> str:
    return _c("33", t)


def cyan(t: str) -> str:
    return _c("36", t)


def bold(t: str) -> str:
    return _c("1", t)


# --------------------------------------------------------------------------- #
# 档案解析
# --------------------------------------------------------------------------- #

class ProfileError(Exception):
    """档案格式错误。"""


def _ascii_field(raw: bytes) -> str:
    """把定长 ASCII 字段转成字符串（去掉 NUL 填充）。"""
    s = raw.split(b"\x00", 1)[0]
    return s.decode("ascii", errors="replace")


def _printable_ascii(raw: bytes) -> bool:
    s = raw.split(b"\x00", 1)[0]
    if not s:
        return False
    return all(0x20 <= b < 0x7F for b in s)


class Profile:
    """一份 ASRock 用户配置档案。

    结构（实测于 X370 Killer SLI / Gaming K4 10.50）::

        0x00  board   定长 32B ASCII，如 "A1818"
        0x20  version 定长 32B ASCII，如 " 10.50"
        0x38  02 00
        0x3A  u32     档案体长度（如 0xD62）
        0x55  u32     Setup 变量长度前缀（如 0x280）
        0x59  ...     **Setup 变量原样副本**（长度 = 上面的 u32）
        之后           档案体其余部分 + 零填充

    注意 0x55 / 0x59 可能会因板型或 BIOS 版本而不同，所以这里不写死，
    而是**自动探测**：在头部区间里找 `u32 L`，L 落在合理范围且不越界。
    """

    def __init__(self, data: bytes, setup_off: int = None, setup_size: int = None):
        if len(data) < 0x100:
            raise ProfileError(T("文件太小（%d 字节），不像 ASRock 配置档案") % len(data))
        self.data = bytearray(data)
        self.board = _ascii_field(self.data[0:HEADER_FIELD_LEN])
        self.version = _ascii_field(self.data[HEADER_FIELD_LEN:2 * HEADER_FIELD_LEN])
        self._detect_note = None

        if not _printable_ascii(self.data[0:HEADER_FIELD_LEN]):
            raise ProfileError(
                T("文件头不是可打印 ASCII，可能不是 ASRock 配置档案"
                "（前 32 字节: %r）") % bytes(self.data[:32])
            )

        self.body_len = struct.unpack_from("<I", self.data, 0x3A)[0]

        if setup_off is None:
            setup_off, setup_size = self._autodetect()
        elif setup_size is None:
            setup_size = struct.unpack_from("<I", self.data, setup_off)[0]
            setup_off += 4

        self.setup_off = setup_off
        self.setup_size = setup_size
        if setup_off + setup_size > len(self.data):
            raise ProfileError(
                T("Setup 块 [0x%X, 0x%X) 超出文件范围（文件 %d 字节）")
                % (setup_off, setup_off + setup_size, len(self.data))
            )
        self._orig = bytes(data)

    # -- 探测 --------------------------------------------------------------- #

    def _autodetect(self):
        """在头部区域定位 Setup 变量的长度前缀。

        为什么不能只靠「4 字节值落在合理区间」：
        Setup 数据**本身**就含 `00 01 00 00`（= 0x100）、`01 01 00 00` 这类序列，
        实测在 0x58/0x5D/0x67 … 会产生十几个假候选，而且因为 Setup 数据
        本身"小字节比例"很高，光按相似度排序反而会把假的排到前面。

        所以这里用两条**结构性**约束一起卡（实测对该格式 100% 有效）：

        1. **长度前缀之前必须有 3 字节零填充**（档案头 ↔ 数据区之间的对齐填充）
           —— 这一条就能排除掉全部「Setup 数据内部」的假候选；
        2. **取偏移最小的那个** —— Setup 是档案体里第一个 length-prefixed blob，
           落在它内部的假候选偏移一定更大。

        另外用「小字节比例」做最后一道合理性校验：AMI 的 Setup 变量是扁平枚举表，
        绝大多数字节只能是 `0x00/0x01/0x02/0x03` 或 `0xFF`（实测真副本约 92–99%）。
        """
        cands = []
        for off in range(0x40, 0x200):
            if off + 4 > len(self.data):
                break
            val = struct.unpack_from("<I", self.data, off)[0]
            if not (SETUP_SIZE_MIN <= val <= SETUP_SIZE_MAX):
                continue
            start = off + 4
            if start + val > len(self.data):
                continue
            if off < 3 or any(self.data[off - 3:off]):
                continue                      # 约束 1：前缀前 3 字节必须是零填充
            blk = self.data[start:start + val]
            if not any(blk):
                continue
            score = sum(1 for b in blk if b <= 3 or b == 0xFF) / float(val)
            if score < 0.75:
                continue
            cands.append((off, val, score))

        if not cands:
            raise ProfileError(
                T("无法自动定位 Setup 变量块。请用 --setup-offset 手工指定\n"
                "  （--setup-offset 要给「长度前缀 + 4」之后的偏移，"
                "即 Setup 变量数据的起始位置）")
            )

        cands.sort(key=lambda c: c[0])        # 约束 2：取最小偏移
        off, val, score = cands[0]
        note = (T("自动探测命中 %d 个候选；选用偏移最小的：长度前缀 @0x%X、"
                "size=0x%X、小字节比例 %.0f%%") % (len(cands), off, val, score * 100))
        if len(cands) > 1:
            note += (T("\n        其余候选：") +
                     ", ".join("@0x%X/0x%X(%.0f%%)" % (o, v, s * 100)
                               for o, v, s in cands[1:6]))
        if val != 0x280:
            note += ("\n        " + yellow(T("注意")) +
                     T("：尺寸不是常见的 0x280，请用 info 核对字段值是否合理"))
        self._detect_note = note
        return off + 4, val

    # -- Setup 读写 --------------------------------------------------------- #

    def get_byte(self, offset: int) -> int:
        self._check_off(offset, 1)
        return self.data[self.setup_off + offset]

    def get_field(self, offset: int, size: int) -> int:
        self._check_off(offset, size)
        return int.from_bytes(self.data[self.setup_off + offset:
                                        self.setup_off + offset + size], "little")

    def set_field(self, offset: int, size: int, value: int):
        self._check_off(offset, size)
        if not 0 <= value < (1 << (8 * size)):
            raise ProfileError(T("值 %d 超出 %d 字节范围") % (value, size))
        self.data[self.setup_off + offset:
                  self.setup_off + offset + size] = value.to_bytes(size, "little")

    def _check_off(self, offset: int, size: int):
        if offset < 0 or offset + size > self.setup_size:
            raise ProfileError(
                T("偏移 0x%X (size %d) 超出 Setup 变量范围 [0, 0x%X)")
                % (offset, size, self.setup_size)
            )

    # -- 输出 --------------------------------------------------------------- #

    def diff(self):
        """返回 [(文件偏移, 旧, 新), ...]"""
        out = []
        for i in range(len(self.data)):
            if self.data[i] != self._orig[i]:
                out.append((i, self._orig[i], self.data[i]))
        return out

    def to_bytes(self) -> bytes:
        return bytes(self.data)

    def describe_field(self, offset: int) -> str:
        size, kind, name = FIELD_BY_OFFSET.get(offset, (1, "raw", None))
        if kind == "llc":
            return name or "Load-Line Calibration"
        return name or T("(未知字段)")


def load(path: str, setup_off: int = None, setup_size: int = None) -> Profile:
    with open(path, "rb") as f:
        data = f.read()
    return Profile(data, setup_off, setup_size)


# --------------------------------------------------------------------------- #
# 板型 / 版本 / 数值健全性检查
# --------------------------------------------------------------------------- #

#: 警告级别
WARN = "warn"
ERROR = "error"


def board_warnings(p: Profile):
    """检查板型 / BIOS 版本，返回 [(level, message), ...]（空列表 = 没问题）。

    设计取向：**宁可多叫一声，也不要静默地把别的板子的偏移当自己的改。**
    本工具的字段表是在 X370 Killer SLI / Gaming K4 10.50 上一条条逆出来的；
    换个板型或换个 BIOS 版本，Setup 变量的布局完全可能变，那时按固定偏移
    改写就是纯粹的盲改 —— 所以这里要拦一下（警告而非硬拦，因为结构相同的
    板型确实可以直接用）。
    """
    out = []
    if not p.board:
        out.append((ERROR, T("档案里读不到板型字段（头 32 字节为空）—— 可能不是 ASRock 配置档案。")))
        return out

    if p.board not in KNOWN_BOARDS:
        out.append((
            WARN,
            T("板型不匹配：档案板型是 %r，本工具只收录了 %s。\n    %s")
            % (p.board,
               T("、").join("%s (%s)" % (k, v) for k, v in sorted(KNOWN_BOARDS.items())),
               unknown_board_hint())
        ))
    else:
        known = KNOWN_VERSIONS.get(p.board)
        if known and p.version not in known:
            out.append((
                WARN,
                T("BIOS 版本未收录：档案版本是 %r，已实测的是 %s。\n"
                "    同板型换版本通常兼容，但 Setup 变量布局有变动的可能，"
                "改前请核对字段值是否合理。") % (p.version, T("、").join(sorted(known)))
            ))

    out.extend(_sanity_warnings(p))
    return out


def _sanity_warnings(p: Profile):
    """用已知的物理约束做一次自检，兜住「板型对但偏移表不对」的情况。

    DDR4 下 VTT_DDR 约为 DRAM Voltage 的一半（DDR4 规范如此，
    实测的档案里正是 1300 mV / 650 mV）。这条关系跟板型无关，
    所以很适合当"偏移表还对不对"的探针。
    """
    out = []
    if p.setup_size < 0x1AA:
        return out
    try:
        dram = p.get_field(0x1A8, 2)
        vtt = p.get_field(0x1A6, 2)
    except ProfileError:
        return out
    if 800 <= dram <= 2000 and abs(vtt * 2 - dram) > 60:
        out.append((
            WARN,
            T("字段自检异常：Setup+0x1A8 DRAM Voltage = %d mV，"
            "但 Setup+0x1A6 VTT_DDR = %d mV；DDR4 下 VTT_DDR 应约为 DRAM 的一半。\n"
            "    这说明字段偏移表与本档案不匹配（板型/BIOS 版本不同），"
            "**请不要继续改写**。") % (dram, vtt)
        ))
    return out


def format_warnings(warns, color: bool = True) -> str:
    """把警告列表渲染成多行文本。"""
    if not warns:
        return ""
    paint = (lambda f, t: f(t)) if color else (lambda f, t: t)
    lines = []
    for level, msg in warns:
        tag = paint(red, T("[版型警告]")) if level == WARN else paint(red, T("[错误]"))
        lines.append("%s %s" % (tag, msg))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 检测：当前 CPU Load-Line Calibration 是几级
# --------------------------------------------------------------------------- #
#
# 为什么需要这一块：CPU LLC 在 Killer 的 BIOS 菜单里**没有条目**，你在界面上看不到、
# 也调不了 —— 想知道"现在到底是几级"，只能把 SPI 读出来分析。
#
# 难在哪：镜像里的 Setup 变量有 150+ 个同源副本（NVRAM compaction / 嵌套 store /
# 变体名 / 不同 size），而且它们**并不是同一个值**。
# 实测 r5：`+0x190` 有 168 个副本是 `00`、9 个是 `03`，而当时真正生效的是 `03`
#   ⇒ 「多数值」是错的判据。
# 用「偏移最大的副本」也不对：`0x0620B7` 是 9 月 13 日 flashrom 写进去的**陈旧副本**，
#   它的值还是 `03`，纯属巧合（r4 时它会让判据给出错误答案）。
#
# 真正的结构（本轮逐字节确认）：
#   * 副本的 NVAR 记录头里有一个 **u24「下一条」指针**（`NVAR+6`，从数据起点算距离）
#   * 所有副本因此串成**单向链**；链尾的标志是头 3 字节 = `ff ff ff`
#   * 链内记录严格按**写入顺序**排列 ⇒ **链尾 = 最近一次写入的状态**
#   * 实测交叉验证：r4 链尾 = `00`(Auto)、r5 链尾 = `03`(Level 3)，
#     与用户"载入 L3 后确实生效"完全一致；指针命中下一条的验证率 147/157、166/177
#
# ⇒ **判据 = 取「活跃链」的链尾记录读字段。**
#
# 活跃链怎么认（单张镜像）：
#   ① 链长 ≥ 2 的才算真 bank（长度为 1 的是散落在外部默认区的孤立副本）
#   ② 优先取「链尾内容在别的链里找不到」的那条 —— 活跃链的尾是**全新状态**；
#      冻结链的尾当初被抄进过活跃链，所以一定能找到
#   ③ 再比「链内 payload 变体数」—— 活跃链记录每一次状态变化，变体明显更多
#      （实测 r5：活跃链 12 种 vs 冻结链 3 种）
#   ④ 给了 `--baseline` 就用「哪条链增长了」判定 —— 这是 **100% 确定**的
#
# ⚠ 诚实的边界：单张镜像靠 ②③ 推断，证据充分但不是数学证明。
#    想 100% 确定，就给一次基线（上次读的镜像，或出厂备份）。

SETUP_SIG = bytes.fromhex("01000001010100000101000101000101")
NVR_TAIL = 0xFFFFFF
NVR_PTR_OFF = 6          # 'NVAR'(4) + u16 size 之后的 u24「下一条」指针
SETUP_COPY_TOL = 120     # 与共识副本的最大允许差异；超过即判为低熵串的巧合命中
CHAIN_MIN_LEN = 2
SPI_MIN_SIZE = 1 << 20   # ≥ 1 MB 视为整片 SPI 镜像，否则当 U 盘配置档案

#: 检测报告里要展示的字段（偏移 → 展示名）
DETECT_FIELDS = [
    (0x190, "CPU Load-Line Calibration"),
    (0x191, "VDDCR_SOC Load-Line Calibration"),
    (0x1A6, "VTT_DDR (mV)"),
    (0x1A8, "DRAM Voltage (mV)"),
    (0x262, "SoC/Uncore OC Mode"),
]


def _consensus(blocks):
    """逐字节取众数 —— 得到「典型 Setup 副本」，用来剔除低熵特征串的巧合命中。"""
    out = bytearray(len(blocks[0]))
    for i in range(len(blocks[0])):
        out[i] = Counter(b[i] for b in blocks).most_common(1)[0][0]
    return bytes(out)


def find_setup_copies(data, size=0x280):
    """全片定位 Setup 变量副本。

    用**内容特征串**扫（不是按记录名 + size 筛 —— 那样会漏掉 90% 的副本）。
    再用「与共识副本的差异字节数」交叉验证，排除低熵串的巧合命中。

    返回 (copies, stats)；copies = 通过校验的数据起点列表。
    """
    hits = [m.start() for m in re.finditer(re.escape(SETUP_SIG), data)]
    hits = [p for p in hits if p + size <= len(data)]
    if not hits:
        return [], {"hits": 0, "kept": 0, "rejected": 0}
    cons = _consensus([data[p:p + size] for p in hits])
    nd = [sum(1 for i in range(size) if data[p + i] != cons[i]) for p in hits]
    copies = [p for p, n in zip(hits, nd) if n <= SETUP_COPY_TOL]
    return copies, {"hits": len(hits), "kept": len(copies),
                    "rejected": len(hits) - len(copies),
                    "min_diff": min(nd), "max_diff": max(nd)}


def build_chains(data, copies):
    """按 NVAR 记录头里的 u24 指针把副本串成链。

    返回 [(chain, ...)]，其中 chain[0] = **链尾 = 最新写入**，chain[-1] = 最旧。
    """
    cset = set(copies)
    ptr = {}
    for p in copies:
        i = data.rfind(b"NVAR", max(0, p - 40), p)
        ptr[p] = (int.from_bytes(data[i + NVR_PTR_OFF:i + NVR_PTR_OFF + 3], "little")
                  if i >= 0 else None)
    pred = {}
    for p, d in ptr.items():
        if d is not None and d != NVR_TAIL and (p + d) in cset:
            pred[p + d] = p

    chains = []
    for t in [p for p, d in ptr.items() if d == NVR_TAIL]:
        cur, ch = t, [t]
        while cur in pred and len(ch) <= 5000:
            cur = pred[cur]
            if cur in ch:
                break
            ch.append(cur)
        chains.append(ch)

    linked = {p for ch in chains for p in ch}
    for p in copies:                     # 没被串进去的散落副本，各算一条单点链
        if p not in linked:
            chains.append([p])
    chains.sort(key=lambda c: -len(c))
    return chains, ptr


def pick_live_chain(data, chains, size=0x280):
    """按上面的 ②③ 选活跃链。返回 (best_info, 全部候选信息)。"""
    pool = [c for c in chains if len(c) >= CHAIN_MIN_LEN] or list(chains)
    payloads = {i: {data[p:p + size] for p in ch} for i, ch in enumerate(pool)}

    info = []
    for i, ch in enumerate(pool):
        tail = data[ch[0]:ch[0] + size]
        seen = any(tail in payloads[j] for j in payloads if j != i)
        info.append({"chain": ch, "tail": ch[0], "oldest": ch[-1], "length": len(ch),
                     "variants": len(payloads[i]), "tail_seen_elsewhere": seen})
    best = sorted(info, key=lambda r: (r["tail_seen_elsewhere"], -r["variants"],
                                       -r["length"]))[0]
    return best, info


def _fmt_llc(off, val):
    if off in LLC_OFFSETS:
        return "%d (%s)" % (val, LLC_OPTIONS.get(val, "?"))
    return str(val)


def _expect_lines(A, v190, expect_path):
    """把「镜像里的值」与一份配置档案比对，结果追加到报告行里。"""
    if not expect_path:
        return
    try:
        ep = Profile(open(expect_path, "rb").read())
        ev = ep.get_byte(0x190)
    except Exception as e:                            # noqa: BLE001
        A("")
        A(T("⚠ 读档案 %s 失败: %s") % (expect_path, e))
        return
    same = (ev == v190)
    A("")
    A(T("=== 与档案 %s 比对 ===") % os.path.basename(expect_path))
    A(T("  档案里的 CPU LLC = %s；镜像里的 = %s ⇒ %s")
      % (_fmt_llc(0x190, ev), _fmt_llc(0x190, v190),
         T("一致 ✔") if same else T("**不一致 ✘**")))


def detect_lines(path, baseline_path=None, expect_path=None, size=0x280):
    """分析一张 SPI 镜像（或 U 盘配置档案），返回 (报告文本行, 结论文本)。

    CLI 与 GUI 共用这一份逻辑。
    """
    data = open(path, "rb").read()
    L = []
    A = L.append

    A(T("文件: %s") % os.path.abspath(path))
    A(T("大小: %d 字节") % len(data))

    # ---- 输入是小档案：直接看内嵌的那一份 Setup ----
    if len(data) < SPI_MIN_SIZE:
        p = Profile(data)
        A(T("类型: U 盘配置档案（板型 %r / BIOS %r）") % (p.board, p.version))
        A(T("Setup 变量位置: 0x%X–0x%X (%d 字节)")
          % (p.setup_off, p.setup_off + p.setup_size - 1, p.setup_size))
        A("")
        A(T("=== 档案里写明的值 ==="))
        for off, name in DETECT_FIELDS:
            if off + 2 <= p.setup_size:
                v = p.get_field(off, p.setup_size and (2 if off in (0x1A6, 0x1A8) else 1))
                A("  Setup+0x%03X  %-38s = %s" % (off, name, _fmt_llc(off, v)))
        v190 = p.get_byte(0x190)
        _expect_lines(A, v190, expect_path)
        A("")
        A(T("⚠ 这是**档案里的值**，不是主板上当前生效的值。"))
        A(T("   要检测当前生效值，请给一张整片 SPI 镜像（AFU 备份出来的 .bin）。"))
        return L, T("档案里的 CPU LLC = %s") % _fmt_llc(0x190, v190)

    # ---- 整片 SPI 镜像 ----
    A(T("类型: 整片 SPI 镜像"))
    A("")
    copies, st = find_setup_copies(data, size)
    A(T("=== Setup 副本定位（内容特征串全片扫描） ==="))
    A(T("  特征串命中 %d 处；与共识副本比对后保留 %d 处（差异 %d–%d 字节）、淘汰 %d 处")
      % (st["hits"], st["kept"], st.get("min_diff", 0), st.get("max_diff", 0),
         st["rejected"]))
    if not copies:
        A(T("  ✘ 一个副本都没找到 —— 这张镜像可能不是本板/本版本的。"))
        return L, T("检测失败：未找到 Setup 副本")
    A(T("  ⚠ 副本数远多于预期是正常的（NVRAM compaction 会留下大量历史版本），"
      "关键是**哪一条最新**"))
    A("")

    chains, ptr = build_chains(data, copies)
    best, info = pick_live_chain(data, chains, size)

    A(T("=== 副本链表（按 NVAR 头的 u24 指针串成；链尾 = 最新写入） ==="))
    A("  %-6s %-12s %-12s %-8s %-9s %s"
      % (T("链长"), T("链尾(最新)"), T("链头(最旧)"), T("变体数"), T("尾是否已在别处出现"), T("区域")))
    for r in info:
        tag = ""
        if r["chain"] is best["chain"]:
            tag = T("  ← 活跃链")
        A("  %-6d 0x%06X     0x%06X     %-8d %-19s %s%s"
          % (r["length"], r["tail"], r["oldest"], r["variants"],
             T("是（⇒ 冻结链）") if r["tail_seen_elsewhere"] else T("否（⇒ 新状态）"),
             T("低区 [11]") if r["tail"] < 0x057000 else T("高区 [10]"), tag))
    A("")

    # ---- 基线对照（确定性判定） ----
    live_tail = best["tail"]
    certain = False
    if baseline_path:
        bdata = open(baseline_path, "rb").read()
        bcopies, _ = find_setup_copies(bdata, size)
        A(T("=== 与基线对照: %s ===") % os.path.basename(baseline_path))
        A(T("  基线副本 %d 处，当前副本 %d 处") % (len(bcopies), len(copies)))
        new = sorted(set(copies) - set(bcopies))
        gone = sorted(set(bcopies) - set(copies))
        A(T("  两次读取之间：新增 %d 处、消失 %d 处（新增 = 这段时间里写进去的）")
          % (len(new), len(gone)))
        if new:
            live_tail = new[-1]
            certain = True
            vals = Counter(data[p + 0x190] for p in new)
            A(T("  新增副本的 +0x190 分布: %s")
              % ", ".join("%s×%d" % (_fmt_llc(0x190, k), v) for k, v in sorted(vals.items())))
            A(T("  ⇒ 最新写入的副本 = @0x%06X（新增副本里偏移最大的那个）") % live_tail)
        else:
            A(T("  ⚠ 没有新增副本 —— 基线可能不是同一个状态，退回单张镜像的推断"))
        A("")

    # ---- 结论 ----
    A(T("=== 结论（读活跃链的链尾记录 @0x%06X） ===") % live_tail)
    tail = data[live_tail:live_tail + size]
    if len(tail) < size:
        return L, T("检测失败：链尾记录不完整")
    for off, name in DETECT_FIELDS:
        if off + 2 <= size:
            v = tail[off] if off not in (0x1A6, 0x1A8) else \
                int.from_bytes(tail[off:off + 2], "little")
            A("  Setup+0x%03X  %-38s = %s" % (off, name, _fmt_llc(off, v)))

    v190 = tail[0x190]
    verdict = T("当前 CPU Load-Line Calibration = %s") % _fmt_llc(0x190, v190)
    A("")
    A("⇒ %s" % verdict)
    if certain:
        A(T("   判定依据：**基线对照** —— 基线里没有、当前镜像里有，且是新增副本中偏移最大的"))
    else:
        A(T("   判定依据：活跃链的链尾（该链尾内容在其它链里找不到 ⇒ 是全新状态；"
          "且链内变体数最多 ⇒ 记录着每次状态变化）"))
        A(T("   ⚠ 单张镜像属**推断**（证据充分但非证明）。想 100% 确定，加 --baseline 给上一次的镜像。"))

    # ---- 与实际设置的交叉核对 ----
    dram = int.from_bytes(tail[0x1A8:0x1AA], "little")
    vtt = int.from_bytes(tail[0x1A6:0x1A8], "little")
    if 800 <= dram <= 2000:
        ok = abs(vtt * 2 - dram) <= 60
        A("")
        A(T("  交叉核对: DRAM %d mV / VTT_DDR %d mV ⇒ %s")
          % (dram, vtt, T("符合 DDR4 的 2:1 关系 ✔") if ok
             else T("**不符合**，这条链可能不是真 Setup 副本 ✘")))

    # ---- 期望值比对 ----
    _expect_lines(A, v190, expect_path)

    return L, verdict


def cmd_detect(args):
    lines, verdict = detect_lines(args.file, args.baseline, args.expect,
                                 args.setup_size or 0x280)
    for ln in lines:
        if ln.startswith("⇒") or T("一致 ✔") in ln or T("不一致 ✘") in ln:
            print(cyan(ln))
        elif "✘" in ln or "⚠" in ln:
            print(yellow(ln))
        else:
            print(ln)
    print()
    print(bold(verdict))
    return 0


# --------------------------------------------------------------------------- #
# 信息渲染（CLI 与 GUI 共用同一份文本）
# --------------------------------------------------------------------------- #

def info_lines(p: Profile, path: str = None) -> list:
    """返回档案信息的纯文本行（无 ANSI），CLI 与 GUI 共用。"""
    L = []
    if path:
        L.append(T("文件: %s") % os.path.abspath(path))
    L.append(T("大小: %d 字节") % len(p.data))
    if p.board in KNOWN_BOARDS:
        L.append(T("板型: %r   ← %s") % (p.board, KNOWN_BOARDS[p.board]))
    else:
        L.append(T("板型: %r   ← ⚠ 未收录的板型") % p.board)
    L.append(T("BIOS 版本: %r%s") % (
        p.version,
        "" if not KNOWN_VERSIONS.get(p.board) or p.version in KNOWN_VERSIONS.get(p.board, set())
        else T("   ← ⚠ 未收录的版本")))
    L.append(T("档案体长度: 0x%X (%d)") % (p.body_len, p.body_len))
    L.append(T("长度前缀位置: 0x%X (%d)") % (p.setup_off - 4, p.setup_off - 4))
    L.append(T("Setup 变量位置: 0x%X–0x%X  (%d 字节)")
             % (p.setup_off, p.setup_off + p.setup_size - 1, p.setup_size))
    if p._detect_note:
        L.append(T("探测: %s") % p._detect_note)
    return L


def field_lines(p: Profile) -> list:
    """返回已知字段当前值（纯文本行）。"""
    L = []
    for off, size, kind, name in KNOWN_FIELDS:
        if off + size > p.setup_size:
            continue
        v = p.get_field(off, size)
        shown = _fmt_value(off, v) if size == 1 else str(v)
        L.append("  Setup+0x%03X  %-46s = %s" % (off, name, shown))
    return L


# --------------------------------------------------------------------------- #
# 命令实现
# --------------------------------------------------------------------------- #

def _fmt_value(offset: int, value: int) -> str:
    if offset in LLC_OFFSETS:
        return "%d (%s)" % (value, LLC_OPTIONS.get(value, "?"))
    return str(value)


def cmd_info(args):
    p = load(args.file, args.setup_offset, args.setup_size)
    for line in info_lines(p, args.file):
        print(line)
    warns = board_warnings(p)
    if warns:
        print()
        print(format_warnings(warns))
    print()
    print(bold(T("已知字段当前值:")))
    for line in field_lines(p):
        print(line)
    if args.dump_setup:
        with open(args.dump_setup, "wb") as f:
            f.write(p.data[p.setup_off:p.setup_off + p.setup_size])
        print(T("\n已导出 Setup 原始数据 → %s") % args.dump_setup)
    return 0


def cmd_get(args):
    p = load(args.file, args.setup_offset, args.setup_size)
    off = _resolve_target(args.target)
    size = FIELD_BY_OFFSET.get(off, (1, "raw", None))[0]
    v = p.get_field(off, size)
    print("Setup+0x%03X  %s = %s" % (off, p.describe_field(off), _fmt_value(off, v)))
    return 0


def _resolve_target(spec: str) -> int:
    """把 'cpu-llc' / '0x190' / '190' 解析成偏移。"""
    key = spec.strip().lower()
    if key in ALIASES:
        return ALIASES[key]
    try:
        return int(key, 16) if not key.startswith("0x") else int(key, 16)
    except ValueError:
        pass
    try:
        return int(key, 0)
    except ValueError:
        raise ProfileError(
            T("无法识别的字段 %r。可用别名: %s；也可以直接给十六进制偏移（如 0x190）")
            % (spec, ", ".join(sorted(ALIASES)))
        )


def build_plan(p: Profile, llc=None, soc_llc=None, raw_bytes=None):
    """构造改动计划 [(offset, size, new_value), ...]（纯逻辑，CLI/GUI 共用）。"""
    plan = []
    if llc is not None:
        _check_llc(llc)
        plan.append((0x190, 1, llc))
    if soc_llc is not None:
        _check_llc(soc_llc)
        plan.append((0x191, 1, soc_llc))
    for off, val in (raw_bytes or []):
        _check_off_spec(p, off)
        if not 0 <= val <= 0xFF:
            raise ProfileError(T("字节值必须在 0..255（Setup+0x%03X 给的是 %d）") % (off, val))
        plan.append((off, 1, val))
    return plan


def _check_off_spec(p: Profile, off: int):
    if off < 0 or off + 1 > p.setup_size:
        raise ProfileError(
            T("偏移 0x%X 超出 Setup 变量范围 [0, 0x%X)") % (off, p.setup_size))


def apply_plan(p: Profile, plan, quiet: bool = False):
    """把计划写进 Profile 对象（内存），返回实际生效的 [(off, size, old, new), ...]。"""
    diffs = []
    for off, size, val in plan:
        old = p.get_field(off, size)
        if size == 1 and old == val:
            if not quiet:
                print(T("%s Setup+0x%03X %s 已经是 %s，跳过") % (
                    yellow(T("[跳过]")), off, p.describe_field(off), _fmt_value(off, val)))
            continue
        p.set_field(off, size, val)
        diffs.append((off, size, old, val))
    return diffs


def cmd_set(args):
    p = load(args.file, args.setup_offset, args.setup_size)

    raw = []
    for item in (args.byte or []):
        off = _resolve_target(item.split("=", 1)[0])
        val = int(item.split("=", 1)[1], 0)
        raw.append((off, val))

    if args.llc is None and args.soc_llc is None and not raw:
        raise ProfileError(T("没有指定要改什么。用 --llc / --soc-llc / --byte，或 -h 看帮助"))

    plan = build_plan(p, args.llc, args.soc_llc, raw)
    diffs = apply_plan(p, plan)
    if not diffs:
        print(T("\n没有任何改动。"))
        return 0

    _report_and_write(p, args, diffs)
    return 0


def _check_llc(v: int):
    if v not in LLC_OPTIONS:
        raise ProfileError(T("Load-Line 等级只能是 0..5（0=Auto），给的是 %d") % v)


def _report_and_write(p: Profile, args, changes):
    warns = board_warnings(p)

    print()
    if warns:
        print(format_warnings(warns))
        print()

    print(bold(T("将要改动:")))
    for off, size, old, val in changes:
        print("  Setup+0x%03X  %-44s %s → %s" % (
            off, p.describe_field(off), _fmt_value(off, old), _fmt_value(off, val)))
    print()

    if args.dry_run:
        _dry_run_diff(changes, p)
        print()
        print(yellow(T("--dry-run：仅预览，未写出任何文件（以上为将会发生的改动）。")))
        return None

    out = args.output
    if out is None:
        base, ext = os.path.splitext(args.file)
        out = base + ".mod" + ext
    if os.path.abspath(out) == os.path.abspath(args.file) and not args.inplace:
        raise ProfileError(
            T("输出路径与输入相同。要覆盖原文件请显式加 --inplace（建议先备份）")
        )

    buf = p.to_bytes()

    # ---- 写出 + 自检（byte-level diff + 一致性检查）----
    with open(out, "wb") as f:
        f.write(buf)

    chk = Profile(buf, p.setup_off, p.setup_size)
    full = [(i, p._orig[i], chk.data[i]) for i in range(len(chk.data))
            if chk.data[i] != p._orig[i]]

    ok = True
    msgs = []
    if len(chk.data) != len(p._orig):
        ok = False
        msgs.append(T("文件长度变了（%d → %d）") % (len(p._orig), len(chk.data)))
    expect = {p.setup_off + off: val for off, size, old, val in changes}
    for i, o, n in full:
        if expect.get(i) != n:
            ok = False
            msgs.append(T("偏移 0x%X 变成 %02X，预期 %s") % (i, n, expect.get(i)))
    got = {(i - p.setup_off): n for i, o, n in full}
    for off, size, old, val in changes:
        if got.get(off) != val:
            ok = False
            msgs.append(T("Setup+0x%03X 未生效") % off)

    print("%s %s" % (bold(T("已写出:")), os.path.abspath(out)))
    print(T("%s %d 字节（与源文件%s）") % (
        bold(T("大小:")), len(buf), green(T("一致")) if len(buf) == len(p._orig) else red(T("不一致"))))
    print()
    _print_diff(full, p)
    print()
    if ok:
        print(green(T("✔ 自检通过：长度一致，且只有上述字节被改动")))
    else:
        print(red(T("✘ 自检失败:")))
        for m in msgs:
            print("   - " + m)
    print()
    print(yellow(T("下一步:")))
    print(T("  1) 把 %s 拷到 FAT32 U 盘根目录") % os.path.basename(out))
    print(T("  2) 进 BIOS → Load User Default from USB flash drive → 选它"))
    print(T("  3) F10 保存退出 → 重启"))
    print(T("  ⚠ BIOS 菜单里不会因此多出条目；换档位就换一个档案载入"))
    return out


def _print_diff(full, p):
    """打印 [文件偏移, 旧, 新] 列表（写出后 / 预览共用）。"""
    print(bold(T("字节级 diff (%d 处):") % len(full)))
    for i, o, n in full:
        so = i - p.setup_off
        tag = "  Setup+0x%03X" % so if 0 <= so < p.setup_size else ""
        print(T("  文件 0x%06X  %02X → %02X%s  %s") % (
            i, o, n, tag, cyan(p.describe_field(so)) if tag else ""))


def _dry_run_diff(changes, p):
    """dry-run 预览：直接列出「会改哪几个文件字节」。"""
    full = [(p.setup_off + off, old, val) for off, size, old, val in changes]
    print(bold(T("将改动的文件字节 (%d 处，仅预览):") % len(full)))
    for i, o, n in full:
        so = i - p.setup_off
        print(T("  文件 0x%06X  %02X → %02X  Setup+0x%03X  %s") % (
            i, o, n, so, cyan(p.describe_field(so))))


def cmd_dump(args):
    p = load(args.file, args.setup_offset, args.setup_size)
    with open(args.out, "wb") as f:
        f.write(p.data[p.setup_off:p.setup_off + p.setup_size])
    print(T("Setup 原始数据（%d 字节）→ %s") % (p.setup_size, args.out))
    print(T("（可用 --setup-offset 0x%X --setup-size 0x%X 把它塞回另一个档案）")
          % (p.setup_off, p.setup_size))
    return 0


def cmd_inject(args):
    p = load(args.file, args.setup_offset, args.setup_size)
    raw = open(args.raw, "rb").read()
    if len(raw) != p.setup_size:
        raise ProfileError(T("原始数据长度 %d 与目标 Setup 块大小 %d 不一致")
                           % (len(raw), p.setup_size))
    p.data[p.setup_off:p.setup_off + p.setup_size] = raw
    diffs = p.diff()
    print(T("将把 %d 字节写入 Setup 块，产生 %d 处改动") % (len(raw), len(diffs)))
    if not diffs:
        print(T("\n没有任何改动。"))
        return 0
    _report_and_write(p, args, [(d[0] - p.setup_off, 1, d[1], d[2]) for d in diffs])
    return 0


def cmd_boards(args):
    print(T("已收录的板型:"))
    for k, v in sorted(KNOWN_BOARDS.items()):
        vers = T("、").join(sorted(KNOWN_VERSIONS.get(k, set()))) or T("(不限版本)")
        print(T("  %-8s %-46s  已实测版本: %s") % (k, v, vers))
    print()
    print(T("未收录也不一定不能用 —— 只要档案结构相同、字段值看着合理即可。"))
    print(T("板型/版本不匹配时工具会打 warning；`info` 也会做一次数值自检"))
    print(T("（用 DRAM Voltage 与 VTT_DDR 的 2:1 关系判断偏移表是否还对得上）。"))
    print(T("若 info 显示的 Setup 变量位置不是 0x59、或自动探测失败，"))
    print(T("请用 --setup-offset / --setup-size 手工指定。"))
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _apply_lang(argv):
    """从 argv 里取 --lang（并摘掉它），返回处理后的 argv。"""
    val, rest = _extract_lang(argv)
    if val and normalize_lang(val) is None:
        # 拼错的 --lang 不该被静默吞掉。这句必须是纯 ASCII —— 它会出现在
        # 「连中文都写不出来」的终端上（那正是自动切成英文的场景）。
        sys.stderr.write("[!] unknown --lang %r (expected zh|en), using auto\n" % val)
    set_lang(val)
    return rest


def build_parser():
    ap = argparse.ArgumentParser(
        prog="asrock_profile",
        description=T("ASRock BIOS 用户配置档案（U 盘档案）编辑器 —— "
                    "用来修改档案里内嵌的 Setup 变量字节。"),
        epilog=T("示例:\n"
               "  asrock_profile                              # 显示这份帮助\n"
               "  asrock_profile info pbo2-test\n"
               "  asrock_profile set  pbo2-test --llc 3\n"
               "  asrock_profile set  pbo2-test --soc-llc 5 -o soc5\n"
               "  asrock_profile set  pbo2-test --byte 0x1B1=1 --dry-run\n"
               "  asrock_profile detect r5.bin --baseline r4.bin\n"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("-V", "--version", action="version",
                    version="asrock_profile %s" % __version__)
    ap.add_argument("--lang", choices=LANGS, metavar="zh|en",
                    help=T("界面/输出语言（默认自动：$ASR_LANG → 终端编码 → 系统语言）。"
                         "写在子命令前后都可以"))

    def add_common(sp):
        sp.add_argument("file", help=T("ASRock 配置档案（BIOS 导出到 U 盘的那个文件）"))
        sp.add_argument("--setup-offset", type=lambda s: int(s, 0), default=None,
                        metavar="N",
                        help=T("手工指定 Setup 变量在文件里的偏移（默认自动探测）"))
        sp.add_argument("--setup-size", type=lambda s: int(s, 0), default=None,
                        metavar="N", help=T("手工指定 Setup 变量大小（默认自动探测）"))

    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("info", help=T("显示档案信息 + 已知字段当前值"))
    add_common(p)
    p.add_argument("--dump-setup", metavar="OUT",
                   help=T("顺带把 Setup 原始数据导出到 OUT"))
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("get", help=T("读取单个字段"))
    add_common(p)
    p.add_argument("target", help=T("字段别名（如 cpu-llc）或十六进制偏移（如 0x190）"))
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("set", help=T("修改字段并写出新档案"))
    add_common(p)
    p.add_argument("--llc", type=int, metavar="0-5",
                   help=T("CPU Load-Line Calibration（Setup+0x190），0=Auto 1..5=Level"))
    p.add_argument("--soc-llc", type=int, metavar="0-5",
                   help=T("VDDCR_SOC Load-Line Calibration（Setup+0x191）"))
    p.add_argument("--byte", action="append", metavar="OFF=VAL",
                   help=T("任意字节，可重复。OFF 支持别名或十六进制（如 0x1B1=1）"))
    p.add_argument("-o", "--output", metavar="FILE",
                   help=T("输出文件（默认 <输入>.mod）"))
    p.add_argument("--inplace", action="store_true",
                   help=T("直接覆盖原文件（强烈建议先备份）"))
    p.add_argument("--dry-run", action="store_true", help=T("只看改动，不写文件"))
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("dump", help=T("导出 Setup 原始数据"))
    add_common(p)
    p.add_argument("out", help=T("输出文件"))
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser("inject", help=T("把 Setup 原始数据塞回档案"))
    add_common(p)
    p.add_argument("raw", help=T("要写入的 Setup 原始数据（大小必须完全一致）"))
    p.add_argument("-o", "--output", metavar="FILE", help=T("输出文件（默认 <输入>.mod）"))
    p.add_argument("--inplace", action="store_true", help=T("直接覆盖原文件"))
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_inject)

    p = sub.add_parser("boards", help=T("列出已收录的板型"))
    p.set_defaults(func=cmd_boards)

    p = sub.add_parser("detect", help=T("检测当前 CPU LLC 是几级（读整片 SPI 镜像）"))
    p.add_argument("file", help=T("整片 SPI 镜像（AFU 备份的 .bin）或 U 盘配置档案"))
    p.add_argument("--baseline", metavar="IMG",
                   help=T("上一次读的镜像 —— 给了就能 100%% 确定（靠「哪条链增长了」判定）"))
    p.add_argument("--expect", metavar="PROFILE",
                   help=T("顺便跟一份配置档案比对（核对载入的值是否真的生效）"))
    p.add_argument("--setup-size", type=lambda x: int(x, 0), default=None,
                   metavar="N", help=T("Setup 变量大小（默认 0x280）"))
    p.set_defaults(func=cmd_detect)

    return ap


SUBCOMMANDS = ("info", "get", "set", "dump", "inject", "boards", "detect")


def _interactive() -> bool:
    """是不是「人在看着」的真实控制台（而不是管道 / 重定向 / 管道调用）。"""
    try:
        return bool(sys.stdout.isatty() and sys.stdin
                    and sys.stdin.isatty())
    except Exception:
        return False


def _quick_help() -> str:
    """非交互环境（管道 / 脚本调用）下的兜底提示。

    正常双击或拖入走的是 run_interactive()，不会到这里。
    """
    return T("""
asrock_profile —— ASRock BIOS 用户配置档案编辑器
（当前不是交互式终端，所以只显示速查。直接双击 exe 会有完整菜单。）

最常用的四件事
--------------
  1. 看档案里现在是什么值
       asrock_profile info  <档案>

  2. 把 CPU 防掉压设成 Level 3（0=Auto，1..5=Level）
       asrock_profile set  <档案> --llc 3

  3. 同时改多个字段
       asrock_profile set  <档案> --soc-llc 5 --byte 0x1B1=1

  4. 看当前生效的 CPU 防掉压是几级（需要整片 SPI 镜像）
       asrock_profile detect <镜像.bin> --baseline <上次.bin>

完整帮助
--------
  asrock_profile -h
  asrock_profile <子命令> -h

把档案文件直接拖到这个 exe 上也行，等价于 `asrock_profile info <档案>`。
""").lstrip()


def _pause():
    """等用户看完再关窗口。管道 / 非交互终端下直接返回，不会卡住脚本。"""
    if not _interactive():
        return
    try:
        print()
        input(T("按回车键关闭…"))
    except (EOFError, KeyboardInterrupt):
        pass


# --------------------------------------------------------------------------- #
# 交互模式（双击 / 拖入时的入口）
# --------------------------------------------------------------------------- #
#
# 为什么要有这个：控制台 exe 被双击时，窗口会在进程退出瞬间关闭。
# 只要程序"跑完就退"，用户什么都看不到 —— 无论打的是帮助还是错误信息。
# 交互模式的价值就是**让窗口留在那里，等用户操作完再关**。

#: 交互模式里可改的字段菜单（编号 → (别名, 说明)）
MENU_ITEMS = [
    ("cpu-llc", "CPU Load-Line Calibration（防掉压，0=Auto 1..5=Level）"),
    ("soc-llc", "VDDCR_SOC Load-Line Calibration（0=Auto 1..5=Level）"),
]


def _find_profiles(start=None):
    """在常见位置找 ASRock 配置档案（按修改时间倒序）。

    只看根目录 + 一层子目录，不做全盘扫描 —— 双击后要立刻出结果。
    """
    import glob
    roots = [start] if start else [
        os.path.expanduser("~"),
        "H:/", "E:/", "D:/", "F:/",
        os.getcwd(),
    ]
    hits, seen = [], set()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        try:
            for pat in ("*.mod", "*.bin", "pbo2*", "cpullc*", "profile*",
                        "*user*default*", "*备份*"):
                for path in glob.glob(os.path.join(root, pat)):
                    rp = os.path.abspath(path)
                    if rp in seen or not os.path.isfile(path):
                        continue
                    seen.add(rp)
                    hits.append((os.path.getmtime(path), rp))
        except Exception:
            continue
    hits.sort(reverse=True)
    return [p for _, p in hits]


def _ask(prompt):
    """问一句并读一行。EOF / Ctrl-C 都返回 None（调用方决定怎么办）。"""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return None


def _show_fields(p: Profile, path=None):
    """列出偏移表：每个已知字段的偏移、名称、当前值。"""
    print()
    print(cyan(T("── 当前偏移表 ──")))
    print("  %-10s %-38s %s" % (T("偏移"), T("字段"), T("当前值")))
    print("  " + "-" * 66)
    for off, size, kind, name in KNOWN_FIELDS:
        if off + size > p.setup_size:
            continue
        try:
            v = p.get_field(off, size)
        except ProfileError:
            continue
        val = _fmt_value(off, v) if size == 1 else str(v)
        if kind == "llc":
            val = "%s (%s)" % (val, LLC_OPTIONS.get(v, "?"))
        print("  0x%03X     %-38s %s" % (off, name[:38], val))
    print("  " + "-" * 66)
    print(T("  Setup 变量：%d 字节 @ 文件偏移 0x%X"
            % (p.setup_size, p.setup_off)))


def _show_warnings(p: Profile):
    warns = board_warnings(p)
    if not warns:
        print(green(T("[OK] 板型 / BIOS 版本 / 字段自检 全部通过")))
        return
    for lv, msg in warns:
        tag = yellow(T("[警告]")) if lv == WARN else red(T("[错误]"))
        print("%s %s" % (tag, msg.replace("\n", "\n         ")))


def run_interactive(initial=None):
    """交互主循环。返回进程退出码。"""
    # stdin 不是终端 ⇒ 有人在脚本里调用，不要进交互（会挂死）
    if not _interactive():
        print(_quick_help())
        return 0

    path = initial
    if not path:
        # ---- 第一步：选档案 ----
        print()
        print(cyan("=" * 68))
        print(cyan(T("  asrock_profile —— ASRock BIOS 配置档案编辑器")))
        print(cyan("=" * 68))
        cands = _find_profiles()
        if cands:
            print()
            print(T("自动找到这些档案（新的在前）："))
            for i, c in enumerate(cands[:12], 1):
                try:
                    sz = os.path.getsize(c)
                except OSError:
                    sz = 0
                print("  %2d) %-58s %d B" % (i, c, sz))
            if len(cands) > 12:
                print(T("      …… 还有 %d 个，输入 s 重新扫描" % (len(cands) - 12)))
        else:
            print(T("没自动找到档案。可以把文件拖到这个窗口里，或直接粘贴路径。"))

        while True:
            ans = _ask("\n" + T("请选档案编号，或粘贴完整路径（q 退出）: "))
            if ans is None or ans.lower() in ("q", "quit", "exit", "退出"):
                print(T("再见。"))
                return 0
            if not ans:
                continue
            if ans.lower() == "s":
                cands = _find_profiles()
                for i, c in enumerate(cands[:12], 1):
                    print("  %2d) %s" % (i, c))
                continue
            if ans.isdigit() and cands:
                n = int(ans)
                if 1 <= n <= len(cands):
                    path = cands[n - 1]
                    break
                print(red(T("编号超出范围")))
                continue
            path = ans
            break

    # ---- 第二步：载入 ----
    try:
        p = load(path)
    except ProfileError as e:
        print(red(T("打不开这个档案: %s") % e))
        return 1
    except FileNotFoundError:
        print(red(T("找不到文件: %s") % path))
        return 1
    except OSError as e:
        print(red(T("读取失败: %s") % e))
        return 1

    print()
    print(green(T("已载入: %s") % os.path.abspath(path)))
    _show_warnings(p)

    # ---- 第三步：菜单循环 ----
    pending = []            # [(offset, value), ...] 累积待写入
    while True:
        _show_fields(p, path)
        print()
        print(cyan(T("── 操作 ──")))
        print("  1) %s" % MENU_ITEMS[0][1])
        print("  2) %s" % MENU_ITEMS[1][1])
        print("  3) %s" % T("改任意字节（偏移 + 新值）"))
        print("  4) %s" % T("取消所有待写入的改动"))
        print("  5) %s" % T("写出到新文件（不覆盖原档案）"))
        print("  6) %s" % T("覆盖原档案（危险）"))
        print("  0) %s" % T("退出"))
        if pending:
            print(yellow(T("待写入 %d 处: %s")
                         % (len(pending),
                            " ".join("0x%X=%02X" % (o, v) for o, v in pending))))

        ans = _ask("\n" + T("选操作编号: ") )
        if ans is None or ans in ("0", "q", "Q", "quit", "exit", "退出"):
            if pending:
                a = _ask(yellow(T("还有 %d 处改动没写出，确定退出？(y/N) " % len(pending))))
                if not a or a.lower() not in ("y", "yes"):
                    continue
            print(T("再见。"))
            return 0

        try:
            if ans in ("1", "2"):
                idx = int(ans) - 1
                spec = MENU_ITEMS[idx][0]
                off = ALIASES[spec]
                print("  " + T("当前值: %s") % _fmt_value(off, p.get_field(off, 1)))
                for v, label in sorted(LLC_OPTIONS.items()):
                    print("     %d = %s" % (v, label))
                raw = _ask(T("要设成几级 (0-5): "))
                if not raw:
                    continue
                val = int(raw, 0)
                _check_llc(val)
                pending = [x for x in pending if x[0] != off]
                pending.append((off, val))
                print(green(T("已加入待写入: Setup+0x%03X = %d (%s)")
                            % (off, val, LLC_OPTIONS[val])))

            elif ans == "3":
                raw = _ask(T("偏移（0x1B1 或别名）: "))
                if not raw:
                    continue
                off = _resolve_target(raw)
                _check_off_spec(p, off)
                raw2 = _ask(T("新值 (0-255): "))
                if not raw2:
                    continue
                val = int(raw2, 0)
                if not 0 <= val <= 0xFF:
                    raise ProfileError(T("字节值必须在 0..255（给的是 %d）") % val)
                pending = [x for x in pending if x[0] != off]
                pending.append((off, val))
                print(green(T("已加入待写入: Setup+0x%03X %s = %d")
                            % (off, p.describe_field(off), val)))

            elif ans == "4":
                pending = []
                print(T("已清空待写入列表。"))

            elif ans in ("5", "6"):
                if not pending:
                    print(red(T("没有待写入的改动。")))
                    continue
                inplace = (ans == "6")
                if inplace:
                    b = _ask(red(T("确定覆盖原档案？输入 YES 确认: ")))
                    if b != "YES":
                        print(T("已取消。"))
                        continue
                # 复用与 `set` 子命令**完全相同**的构造 / 写出 / 自检逻辑
                import argparse
                args = argparse.Namespace(
                    file=path,
                    output=None,
                    inplace=inplace,
                    dry_run=False,
                )
                try:
                    prof = load(path)
                    plan = build_plan(prof, raw_bytes=list(pending))
                    diffs = apply_plan(prof, plan, quiet=True)
                    set_color(False)
                    try:
                        out = _report_and_write(prof, args, diffs)
                    finally:
                        set_color(sys.stdout.isatty())
                except (ProfileError, OSError) as e:
                    print(red(T("写出失败: %s") % e))
                    continue
                if not diffs:
                    print(yellow(T("指定的值与当前值相同，无需写出。")))
                    continue
                # 写完重新载入，界面同步到新状态
                path = out
                p = load(path)
                pending = []
                print(green(T("界面已切换到新档案: %s") % os.path.abspath(path)))
            else:
                print(yellow(T("没有这个选项。")))

        except ValueError as e:
            print(red(T("输入不是数字: %s") % e))
        except ProfileError as e:
            print(red(T("操作失败: %s") % e))
        except OSError as e:
            print(red(T("文件操作失败: %s") % e))


def main(argv=None):
    argv = list(sys.argv[1:]) if argv is None else list(argv)

    # 先定语言再建 parser：argparse 的 help 是**建 parser 时**取译文的，
    # 顺序反了的话 -h 永远显示中文。
    argv = _apply_lang(argv)
    encoding_hint()

    # 把档案直接拖到 exe 上（argv[0] 是存在的文件路径、且不是子命令）
    # ⇒ 打开它并进入交互模式。
    # 只对「确实存在的文件」生效，所以子命令打错字时仍走 argparse 给出正确报错。
    if (argv and not argv[0].startswith("-")
            and argv[0] not in SUBCOMMANDS and os.path.exists(argv[0])):
        return run_interactive(initial=argv[0])

    # 无参数 = 双击启动 ⇒ 交互模式。
    # 非交互终端（管道 / 脚本调用）下 run_interactive 会自动退回打印速查。
    if not argv:
        return run_interactive()

    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "cmd", None):
        ap.print_help()
        return 0

    try:
        return args.func(args)
    except ProfileError as e:
        print(red(T("错误: ")) + str(e), file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(red(T("错误: ")) + T("找不到文件 %s") % e.filename, file=sys.stderr)
        return 1
    except OSError as e:
        # 权限不足 / 目标是目录 / 磁盘满 / 文件被别的进程占用 —— Windows 与 POSIX 都会碰到
        print(red(T("错误: ")) + "%s: %s" % (type(e).__name__, e), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        _pause()
        return 130



# --------------------------------------------------------------------------- #
# i18n 译文表（key = 中文原文，value = 英文）
# --------------------------------------------------------------------------- #
#   自动生成 / 人工逐条校对。`T()` 只做一次 dict 查询（O(1)），
#   查不到就返回原文 ⇒ 漏译最多显示成中文，不会崩。
#   改文案时的规矩：改了中文 key，就要同步改这里的 key（否则那条会退回中文显示）。
EN = {
    # 其余候选：
    '\n        其余候选：': '\n        other candidates: ',
    # asrock_profile —— ASRock BIOS 
    '\nasrock_profile —— ASRock BIOS 用户配置档案编辑器\n（当前不是交互式终端，所以只显示速查。直接双击 exe 会有完整菜单。）\n\n最常用的四件事\n--------------\n  1. 看档案里现在是什么值\n       asrock_profile info  <档案>\n\n  2. 把 CPU 防掉压设成 Level 3（0=Auto，1..5=Level）\n       asrock_profile set  <档案> --llc 3\n\n  3. 同时改多个字段\n       asrock_profile set  <档案> --soc-llc 5 --byte 0x1B1=1\n\n  4. 看当前生效的 CPU 防掉压是几级（需要整片 SPI 镜像）\n       asrock_profile detect <镜像.bin> --baseline <上次.bin>\n\n完整帮助\n--------\n  asrock_profile -h\n  asrock_profile <子命令> -h\n\n把档案文件直接拖到这个 exe 上也行，等价于 `asrock_profile info <档案>`。\n': "\nasrock_profile - ASRock BIOS profile editor\n(not an interactive terminal, so this is a cheat-sheet only;\n double-click the exe to get the full menu)\n\nThe four things you'll do most\n-----------------------------\n  1. See what the profile currently holds\n       asrock_profile info  <profile>\n\n  2. Set CPU Load-Line Calibration to Level 3 (0=Auto, 1..5=Level)\n       asrock_profile set  <profile> --llc 3\n\n  3. Change several fields at once\n       asrock_profile set  <profile> --soc-llc 5 --byte 0x1B1=1\n\n  4. Check which CPU LLC level is actually active (needs a full SPI image)\n       asrock_profile detect <image.bin> --baseline <previous.bin>\n\nFull help\n---------\n  asrock_profile -h\n  asrock_profile <subcommand> -h\n\nDragging a profile onto the exe works too - it opens the interactive menu.\n",
    # 已导出 Setup 原始数据 → %s
    '\n已导出 Setup 原始数据 → %s': '\nExported raw Setup data → %s',
    # 没有任何改动。
    '\n没有任何改动。': '\nNo changes.',
    # ← ⚠ 未收录的版本
    '   ← ⚠ 未收录的版本': '   ← ⚠ version not in the known list',
    # ⚠ 单张镜像属**推断**（证据充分但非证明）。想 100%
    '   ⚠ 单张镜像属**推断**（证据充分但非证明）。想 100% 确定，加 --baseline 给上一次的镜像。': '   ⚠ A single image only gives an **inference** (strong evidence, not proof). For 100% certainty, add --baseline with the previous image.',
    # 判定依据：**基线对照** —— 基线里没有、当前镜像里有，
    '   判定依据：**基线对照** —— 基线里没有、当前镜像里有，且是新增副本中偏移最大的': '   Basis: **baseline comparison** — absent in the baseline, present now, and the highest-addressed of the new copies',
    # 判定依据：活跃链的链尾（该链尾内容在其它链里找不到 ⇒ 是全
    '   判定依据：活跃链的链尾（该链尾内容在其它链里找不到 ⇒ 是全新状态；且链内变体数最多 ⇒ 记录着每次状态变化）': '   Basis: the tail of the live chain (its content appears in no other chain ⇒ it is a brand-new state; and it holds the most variants ⇒ it records every state change)',
    # 要检测当前生效值，请给一张整片 SPI 镜像（AFU 备份出
    '   要检测当前生效值，请给一张整片 SPI 镜像（AFU 备份出来的 .bin）。': '   To detect the active value, pass a full SPI image (a .bin backed up with AFU).',
    # %-8s %-46s  已实测版本: %s
    '  %-8s %-46s  已实测版本: %s': '  %-8s %-46s  tested versions: %s',
    # 1) 把 %s 拷到 FAT32 U 盘根目录
    '  1) 把 %s 拷到 FAT32 U 盘根目录': '  1) Copy %s to the root of a FAT32 USB stick',
    # 2) 进 BIOS → Load User Default 
    '  2) 进 BIOS → Load User Default from USB flash drive → 选它': '  2) Enter BIOS → Load User Default from USB flash drive → pick it',
    # 3) F10 保存退出 → 重启
    '  3) F10 保存退出 → 重启': '  3) F10 to save and exit → reboot',
    # asrock_profile —— ASRock BIOS 
    '  asrock_profile —— ASRock BIOS 配置档案编辑器': '  asrock_profile - ASRock BIOS profile editor',
    # ← 活跃链
    '  ← 活跃链': '  ← live chain',
    # ⇒ 最新写入的副本 = @0x%06X（新增副本里偏移最大的
    '  ⇒ 最新写入的副本 = @0x%06X（新增副本里偏移最大的那个）': '  ⇒ newest copy written = @0x%06X (the highest-addressed of the new copies)',
    # ⚠ BIOS 菜单里不会因此多出条目；换档位就换一个档案载入
    '  ⚠ BIOS 菜单里不会因此多出条目；换档位就换一个档案载入': '  ⚠ This will NOT add a BIOS menu entry; to change level, load a different profile',
    # ⚠ 副本数远多于预期是正常的（NVRAM compactio
    '  ⚠ 副本数远多于预期是正常的（NVRAM compaction 会留下大量历史版本），关键是**哪一条最新**': '  ⚠ Far more copies than expected is normal (NVRAM compaction leaves plenty of historical versions); what matters is **which one is newest**',
    # ⚠ 没有新增副本 —— 基线可能不是同一个状态，退回单张镜像
    '  ⚠ 没有新增副本 —— 基线可能不是同一个状态，退回单张镜像的推断': '  ⚠ No new copies — the baseline may not be the same state; falling back to single-image inference',
    # ✘ 一个副本都没找到 —— 这张镜像可能不是本板/本版本的。
    '  ✘ 一个副本都没找到 —— 这张镜像可能不是本板/本版本的。': '  ✘ Not a single copy found — this image is probably not from this board/version.',
    # 两次读取之间：新增 %d 处、消失 %d 处（新增 = 这段
    '  两次读取之间：新增 %d 处、消失 %d 处（新增 = 这段时间里写进去的）': '  Between the two reads: %d new, %d gone (new = written in that window)',
    # 交叉核对: DRAM %d mV / VTT_DDR %d 
    '  交叉核对: DRAM %d mV / VTT_DDR %d mV ⇒ %s': '  Cross-check: DRAM %d mV / VTT_DDR %d mV ⇒ %s',
    # 基线副本 %d 处，当前副本 %d 处
    '  基线副本 %d 处，当前副本 %d 处': '  baseline copies: %d, current copies: %d',
    # 文件 0x%06X  %02X → %02X  Setup+
    '  文件 0x%06X  %02X → %02X  Setup+0x%03X  %s': '  file 0x%06X  %02X → %02X  Setup+0x%03X  %s',
    # 文件 0x%06X  %02X → %02X%s  %s
    '  文件 0x%06X  %02X → %02X%s  %s': '  file 0x%06X  %02X → %02X%s  %s',
    # 新增副本的 +0x190 分布: %s
    '  新增副本的 +0x190 分布: %s': '  +0x190 distribution of the new copies: %s',
    # 档案里的 CPU LLC = %s；镜像里的 = %s ⇒ 
    '  档案里的 CPU LLC = %s；镜像里的 = %s ⇒ %s': '  CPU LLC in the profile = %s; in the image = %s ⇒ %s',
    # 特征串命中 %d 处；与共识副本比对后保留 %d 处（差异 
    '  特征串命中 %d 处；与共识副本比对后保留 %d 处（差异 %d–%d 字节）、淘汰 %d 处': '  signature hits: %d; after comparison with the consensus copy, kept %d (differences %d–%d bytes), rejected %d',
    # %s %d 字节（与源文件%s）
    '%s %d 字节（与源文件%s）': '%s %d bytes (source file: %s)',
    # %s Setup+0x%03X %s 已经是 %s，跳过
    '%s Setup+0x%03X %s 已经是 %s，跳过': '%s Setup+0x%03X %s is already %s, skipping',
    # (不改)
    '(不改)': '(no change)',
    # (不限版本)
    '(不限版本)': '(any version)',
    # (未知字段)
    '(未知字段)': '(unknown field)',
    # **不一致 ✘**
    '**不一致 ✘**': '**MISMATCH ✘**',
    # **不符合**，这条链可能不是真 Setup 副本 ✘
    '**不符合**，这条链可能不是真 Setup 副本 ✘': '**does NOT match**, this chain may not be a real Setup copy ✘',
    # --dry-run：仅预览，未写出任何文件（以上为将会发生的
    '--dry-run：仅预览，未写出任何文件（以上为将会发生的改动）。': '--dry-run: preview only, nothing was written (the above is what would happen).',
    # === Setup 副本定位（内容特征串全片扫描） ===
    '=== Setup 副本定位（内容特征串全片扫描） ===': '=== Locating Setup copies (whole-image scan for the content signature) ===',
    # === 与基线对照: %s ===
    '=== 与基线对照: %s ===': '=== Compared with the baseline: %s ===',
    # === 与档案 %s 比对 ===
    '=== 与档案 %s 比对 ===': '=== Compared with profile %s ===',
    # === 副本链表（按 NVAR 头的 u24 指针串成；链尾
    '=== 副本链表（按 NVAR 头的 u24 指针串成；链尾 = 最新写入） ===': '=== Copy chains (linked by the u24 pointer in each NVAR header; tail = newest write) ===',
    # === 档案里写明的值 ===
    '=== 档案里写明的值 ===': '=== Value as stored in the profile ===',
    # === 结论（读活跃链的链尾记录 @0x%06X） ===
    '=== 结论（读活跃链的链尾记录 @0x%06X） ===': "=== Verdict (reading the live chain's tail record @0x%06X) ===",
    # ASRock BIOS 用户配置档案（U 盘档案）编辑器 —
    'ASRock BIOS 用户配置档案（U 盘档案）编辑器 —— 用来修改档案里内嵌的 Setup 变量字节。': 'ASRock BIOS profile (USB config file) editor — for editing the Setup variable bytes embedded in such a profile.',
    # ASRock 配置档案（BIOS 导出到 U 盘的那个文件）
    'ASRock 配置档案（BIOS 导出到 U 盘的那个文件）': 'ASRock profile (the file the BIOS exports to a USB stick)',
    # BIOS 版本: %r%s
    'BIOS 版本: %r%s': 'BIOS version: %r%s',
    # BIOS 版本未收录：档案版本是 %r，已实测的是 %s。
    'BIOS 版本未收录：档案版本是 %r，已实测的是 %s。\n    同板型换版本通常兼容，但 Setup 变量布局有变动的可能，改前请核对字段值是否合理。': 'BIOS version not in the known list: the profile says %r, tested: %s.\n    A different version of the same board is usually compatible, but the Setup variable layout may have moved — check the field values first.',
    # CPU Load-Line Calibration（Setu
    'CPU Load-Line Calibration（Setup+0x190），0=Auto 1..5=Level': 'CPU Load-Line Calibration (Setup+0x190), 0=Auto 1..5=Level',
    # Load-Line 等级只能是 0..5（0=Auto），给
    'Load-Line 等级只能是 0..5（0=Auto），给的是 %d': 'Load-Line level must be 0..5 (0=Auto), got %d',
    # Setup 原始数据（%d 字节）→ %s
    'Setup 原始数据（%d 字节）→ %s': 'Raw Setup data (%d bytes) → %s',
    # Setup 变量位置: 0x%X–0x%X  (%d 字节)
    'Setup 变量位置: 0x%X–0x%X  (%d 字节)': 'Setup variable: 0x%X–0x%X  (%d bytes)',
    # Setup 变量位置: 0x%X–0x%X (%d 字节)
    'Setup 变量位置: 0x%X–0x%X (%d 字节)': 'Setup variable: 0x%X–0x%X (%d bytes)',
    # Setup 变量大小（默认 0x280）
    'Setup 变量大小（默认 0x280）': 'Setup variable size (default 0x280)',
    # Setup 块 [0x%X, 0x%X) 超出文件范围（文件
    'Setup 块 [0x%X, 0x%X) 超出文件范围（文件 %d 字节）': 'Setup block [0x%X, 0x%X) is outside the file (%d bytes)',
    # Setup+0x%03X 未生效
    'Setup+0x%03X 未生效': 'Setup+0x%03X did not take effect',
    # VDDCR_SOC Load-Line Calibratio
    'VDDCR_SOC Load-Line Calibration（Setup+0x191）': 'VDDCR_SOC Load-Line Calibration (Setup+0x191)',
    # [OK] 板型 / BIOS 版本 / 字段自检 全部通过
    '[OK] 板型 / BIOS 版本 / 字段自检 全部通过': '[OK] board / BIOS version / field self-check all passed',
    # [版型警告]
    '[版型警告]': '[board warning]',
    # [警告]
    '[警告]': '[warn]',
    # [跳过]
    '[跳过]': '[skip]',
    # [错误]
    '[错误]': '[error]',
    # ── 当前偏移表 ──
    '── 当前偏移表 ──': '-- Current values --',
    # ── 操作 ──
    '── 操作 ──': '-- Actions --',
    # ⚠ 读档案 %s 失败: %s
    '⚠ 读档案 %s 失败: %s': '⚠ Failed to read profile %s: %s',
    # ⚠ 这是**档案里的值**，不是主板上当前生效的值。
    '⚠ 这是**档案里的值**，不是主板上当前生效的值。': '⚠ This is the value **inside the profile**, not what is currently active on the board.',
    # ✔ 自检通过：长度一致，且只有上述字节被改动
    '✔ 自检通过：长度一致，且只有上述字节被改动': '✔ Self-check passed: same length, and only the bytes listed above were changed',
    # ✘ 自检失败:
    '✘ 自检失败:': '✘ Self-check failed:',
    # 、
    '、': ', ',
    # 一致
    '一致': 'same',
    # 一致 ✔
    '一致 ✔': 'match ✔',
    # 上一次读的镜像 —— 给了就能 100%% 确定（靠「哪条链
    '上一次读的镜像 —— 给了就能 100%% 确定（靠「哪条链增长了」判定）': 'The previously read image — supplying it makes the verdict 100%% certain (decided by which chain grew)',
    # 下一步:
    '下一步:': 'Next steps:',
    # 不一致
    '不一致': 'differs',
    # 不一致 ✘
    '不一致 ✘': 'does not match ✘',
    # 任意字节，可重复。OFF 支持别名或十六进制（如 0x1B1
    '任意字节，可重复。OFF 支持别名或十六进制（如 0x1B1=1）': 'Any byte, repeatable. OFF accepts an alias or hex (e.g. 0x1B1=1)',
    # 低区 [11]
    '低区 [11]': 'low region [11]',
    # 修改字段并写出新档案
    '修改字段并写出新档案': 'Modify fields and write a new profile',
    # 值 %d 超出 %d 字节范围
    '值 %d 超出 %d 字节范围': 'Value %d does not fit in %d byte(s)',
    # 偏移
    '偏移': 'Offset',
    # 偏移 0x%X (size %d) 超出 Setup 变量范
    '偏移 0x%X (size %d) 超出 Setup 变量范围 [0, 0x%X)': 'Offset 0x%X (size %d) is outside the Setup variable range [0, 0x%X)',
    # 偏移 0x%X 变成 %02X，预期 %s
    '偏移 0x%X 变成 %02X，预期 %s': 'Offset 0x%X became %02X, expected %s',
    # 偏移 0x%X 超出 Setup 变量范围 [0, 0x%X
    '偏移 0x%X 超出 Setup 变量范围 [0, 0x%X)': 'Offset 0x%X is outside the Setup variable range [0, 0x%X)',
    # 偏移（0x1B1 或别名）:
    '偏移（0x1B1 或别名）: ': 'Offset (0x1B1 or an alias): ',
    # 再见。
    '再见。': 'Bye.',
    # 写出到新文件（不覆盖原档案）
    '写出到新文件（不覆盖原档案）': 'Write to a NEW file (leaves the source alone)',
    # 写出失败: %s
    '写出失败: %s': 'Write failed: %s',
    # 列出已收录的板型
    '列出已收录的板型': 'List known boards',
    # 区域
    '区域': 'region',
    # 原始数据长度 %d 与目标 Setup 块大小 %d 不一致
    '原始数据长度 %d 与目标 Setup 块大小 %d 不一致': 'Raw data length %d does not match the target Setup block size %d',
    # 取消所有待写入的改动
    '取消所有待写入的改动': 'Discard all pending changes',
    # 变体数
    '变体数': 'variants',
    # 只看改动，不写文件
    '只看改动，不写文件': 'Only show the changes, write nothing',
    # 否（⇒ 新状态）
    '否（⇒ 新状态）': 'no (⇒ new state)',
    # 大小:
    '大小:': 'Size:',
    # 大小: %d 字节
    '大小: %d 字节': 'Size: %d bytes',
    # 字段
    '字段': 'Field',
    # 字段偏移表只对已实测的板型负责。请先用 `info` 核对几
    '字段偏移表只对已实测的板型负责。请先用 `info` 核对几条已知量是否合理：\n    Setup+0x1A8 DRAM Voltage 应是合理内存电压（如 1200–1500 mV），\n    且 Setup+0x1A6 VTT_DDR 约为它的一半。\n    数值明显不合理 ⇒ **不要改**，先把 `info` 输出贴到 issue 补字段表。': 'The field offset table is only guaranteed for boards that were actually tested. First check a few known values with `info`:\n    Setup+0x1A8 DRAM Voltage should be a sane memory voltage (say 1200–1500 mV),\n    and Setup+0x1A6 VTT_DDR should be about half of it.\n    Obviously wrong numbers ⇒ **do not write** — open an issue and paste your `info` output so the table can be extended.',
    # 字段别名（如 cpu-llc）或十六进制偏移（如 0x190
    '字段别名（如 cpu-llc）或十六进制偏移（如 0x190）': 'Field alias (e.g. cpu-llc) or hex offset (e.g. 0x190)',
    # 字段自检异常：Setup+0x1A8 DRAM Voltag
    '字段自检异常：Setup+0x1A8 DRAM Voltage = %d mV，但 Setup+0x1A6 VTT_DDR = %d mV；DDR4 下 VTT_DDR 应约为 DRAM 的一半。\n    这说明字段偏移表与本档案不匹配（板型/BIOS 版本不同），**请不要继续改写**。': 'Field self-check failed: Setup+0x1A8 DRAM Voltage = %d mV but Setup+0x1A6 VTT_DDR = %d mV; on DDR4, VTT_DDR should be about half of DRAM.\n    This means the field offset table does not match this profile (different board/BIOS version) — **please stop and do not write**.',
    # 字节值必须在 0..255（Setup+0x%03X 给的是
    '字节值必须在 0..255（Setup+0x%03X 给的是 %d）': 'Byte value must be 0..255 (Setup+0x%03X was given %d)',
    # 字节值必须在 0..255（给的是 %d）
    '字节值必须在 0..255（给的是 %d）': 'Byte value must be 0..255 (got %d)',
    # 字节级 diff (%d 处):
    '字节级 diff (%d 处):': 'Byte-level diff (%d change(s)):',
    # 导出 Setup 原始数据
    '导出 Setup 原始数据': 'Export the raw Setup data',
    # 将把 %d 字节写入 Setup 块，产生 %d 处改动
    '将把 %d 字节写入 Setup 块，产生 %d 处改动': 'Will write %d bytes into the Setup block, producing %d change(s)',
    # 将改动的文件字节 (%d 处，仅预览):
    '将改动的文件字节 (%d 处，仅预览):': 'File bytes that would change (%d change(s), preview only):',
    # 将要改动:
    '将要改动:': 'About to change:',
    # 尾是否已在别处出现
    '尾是否已在别处出现': 'tail seen elsewhere?',
    # 已写出:
    '已写出:': 'Written:',
    # 已加入待写入: Setup+0x%03X %s = %d
    '已加入待写入: Setup+0x%03X %s = %d': 'Queued: Setup+0x%03X %s = %d',
    # 已加入待写入: Setup+0x%03X = %d (%s)
    '已加入待写入: Setup+0x%03X = %d (%s)': 'Queued: Setup+0x%03X = %d (%s)',
    # 已取消。
    '已取消。': 'Cancelled.',
    # 已收录的板型:
    '已收录的板型:': 'Known boards:',
    # 已清空待写入列表。
    '已清空待写入列表。': 'Pending changes cleared.',
    # 已知字段当前值:
    '已知字段当前值:': 'Known field values:',
    # 已载入: %s
    '已载入: %s': 'Loaded: %s',
    # 当前 CPU Load-Line Calibration =
    '当前 CPU Load-Line Calibration = %s': 'Current CPU Load-Line Calibration = %s',
    # 当前值
    '当前值': 'Value',
    # 当前值: %s
    '当前值: %s': 'Current: %s',
    # 待写入 %d 处: %s
    '待写入 %d 处: %s': '%d pending change(s): %s',
    # 手工指定 Setup 变量在文件里的偏移（默认自动探测）
    '手工指定 Setup 变量在文件里的偏移（默认自动探测）': 'Set the Setup variable offset manually (auto-detected by default)',
    # 手工指定 Setup 变量大小（默认自动探测）
    '手工指定 Setup 变量大小（默认自动探测）': 'Set the Setup variable size manually (auto-detected by default)',
    # 打不开这个档案: %s
    '打不开这个档案: %s': 'Cannot open that profile: %s',
    # 找不到文件 %s
    '找不到文件 %s': 'File not found: %s',
    # 找不到文件: %s
    '找不到文件: %s': 'File not found: %s',
    # 把 Setup 原始数据塞回档案
    '把 Setup 原始数据塞回档案': 'Inject raw Setup data back into a profile',
    # 指定的值与当前值相同，无需写出。
    '指定的值与当前值相同，无需写出。': 'Those values equal the current ones; nothing to write.',
    # 按回车键关闭…
    '按回车键关闭…': 'Press Enter to close...',
    # 探测: %s
    '探测: %s': 'Detection: %s',
    # 操作失败: %s
    '操作失败: %s': 'Action failed: %s',
    # 改任意字节（偏移 + 新值）
    '改任意字节（偏移 + 新值）': 'Change an arbitrary byte (offset + value)',
    # 整片 SPI 镜像（AFU 备份的 .bin）或 U 盘配置
    '整片 SPI 镜像（AFU 备份的 .bin）或 U 盘配置档案': 'Full SPI image (a .bin backed up by AFU) or a USB config profile',
    # 文件: %s
    '文件: %s': 'File: %s',
    # 文件太小（%d 字节），不像 ASRock 配置档案
    '文件太小（%d 字节），不像 ASRock 配置档案': 'File is too small (%d bytes) to be an ASRock profile',
    # 文件头不是可打印 ASCII，可能不是 ASRock 配置档
    '文件头不是可打印 ASCII，可能不是 ASRock 配置档案（前 32 字节: %r）': 'The file header is not printable ASCII — this may not be an ASRock profile (first 32 bytes: %r)',
    # 文件操作失败: %s
    '文件操作失败: %s': 'File operation failed: %s',
    # 文件长度变了（%d → %d）
    '文件长度变了（%d → %d）': 'File length changed (%d → %d)',
    # 新值 (0-255):
    '新值 (0-255): ': 'New value (0-255): ',
    # 无法自动定位 Setup 变量块。请用 --setup-of
    '无法自动定位 Setup 变量块。请用 --setup-offset 手工指定\n  （--setup-offset 要给「长度前缀 + 4」之后的偏移，即 Setup 变量数据的起始位置）': 'Cannot locate the Setup variable block automatically. Pass --setup-offset manually\n  (--setup-offset takes the offset *after* the 4-byte length prefix, i.e. where the Setup data starts)',
    # 无法识别的字段 %r。可用别名: %s；也可以直接给十六进制
    '无法识别的字段 %r。可用别名: %s；也可以直接给十六进制偏移（如 0x190）': 'Unrecognised field %r. Aliases: %s; you can also pass a hex offset (e.g. 0x190)',
    # 是（⇒ 冻结链）
    '是（⇒ 冻结链）': 'yes (⇒ frozen chain)',
    # 显示档案信息 + 已知字段当前值
    '显示档案信息 + 已知字段当前值': 'Show profile info + current values of known fields',
    # 未收录也不一定不能用 —— 只要档案结构相同、字段值看着合理
    '未收录也不一定不能用 —— 只要档案结构相同、字段值看着合理即可。': 'Not being listed does not mean it will not work — as long as the profile layout matches and the field values look sane.',
    # 板型/版本不匹配时工具会打 warning；`info` 也
    '板型/版本不匹配时工具会打 warning；`info` 也会做一次数值自检': 'On a board/version mismatch the tool prints a warning; `info` also runs a numeric self-check',
    # 板型: %r   ← %s
    '板型: %r   ← %s': 'Board: %r   ← %s',
    # 板型: %r   ← ⚠ 未收录的板型
    '板型: %r   ← ⚠ 未收录的板型': 'Board: %r   ← ⚠ board not in the known list',
    # 板型不匹配：档案板型是 %r，本工具只收录了 %s。
    '板型不匹配：档案板型是 %r，本工具只收录了 %s。\n    %s': 'Board mismatch: the profile says %r, but this tool only knows %s.\n    %s',
    # 档案体长度: 0x%X (%d)
    '档案体长度: 0x%X (%d)': 'Profile body length: 0x%X (%d)',
    # 档案里的 CPU LLC = %s
    '档案里的 CPU LLC = %s': 'CPU LLC in the profile = %s',
    # 档案里读不到板型字段（头 32 字节为空）—— 可能不是 A
    '档案里读不到板型字段（头 32 字节为空）—— 可能不是 ASRock 配置档案。': 'No board field in the profile (the first 32 bytes are empty) — this may not be an ASRock profile.',
    # 检测失败：未找到 Setup 副本
    '检测失败：未找到 Setup 副本': 'Detection failed: no Setup copy found',
    # 检测失败：链尾记录不完整
    '检测失败：链尾记录不完整': 'Detection failed: the tail record is incomplete',
    # 检测当前 CPU LLC 是几级（读整片 SPI 镜像）
    '检测当前 CPU LLC 是几级（读整片 SPI 镜像）': 'Detect the current CPU LLC level (reads a full SPI image)',
    # 没有待写入的改动。
    '没有待写入的改动。': 'Nothing pending.',
    # 没有指定要改什么。用 --llc / --soc-llc /
    '没有指定要改什么。用 --llc / --soc-llc / --byte，或 -h 看帮助': 'Nothing to change. Use --llc / --soc-llc / --byte, or -h for help',
    # 没有这个选项。
    '没有这个选项。': 'No such option.',
    # 没自动找到档案。可以把文件拖到这个窗口里，或直接粘贴路径。
    '没自动找到档案。可以把文件拖到这个窗口里，或直接粘贴路径。': 'No profiles found automatically. Drag a file into this window, or paste the path.',
    # 注意
    '注意': 'Note',
    # 界面/输出语言（默认自动：$ASR_LANG → 终端编码 
    '界面/输出语言（默认自动：$ASR_LANG → 终端编码 → 系统语言）。写在子命令前后都可以': 'UI/output language (default: auto — $ASR_LANG → terminal encoding → system locale). May be placed before or after the subcommand',
    # 界面已切换到新档案: %s
    '界面已切换到新档案: %s': 'Now showing the newly written profile: %s',
    # 直接覆盖原文件
    '直接覆盖原文件': 'Overwrite the source file',
    # 直接覆盖原文件（强烈建议先备份）
    '直接覆盖原文件（强烈建议先备份）': 'Overwrite the source file (a backup is strongly recommended)',
    # 确定覆盖原档案？输入 YES 确认:
    '确定覆盖原档案？输入 YES 确认: ': 'Really overwrite the source? Type YES: ',
    # 示例:
    '示例:\n  asrock_profile                              # 显示这份帮助\n  asrock_profile info pbo2-test\n  asrock_profile set  pbo2-test --llc 3\n  asrock_profile set  pbo2-test --soc-llc 5 -o soc5\n  asrock_profile set  pbo2-test --byte 0x1B1=1 --dry-run\n  asrock_profile detect r5.bin --baseline r4.bin\n': 'Examples:\n  asrock_profile                              # show this help\n  asrock_profile info pbo2-test\n  asrock_profile set  pbo2-test --llc 3\n  asrock_profile set  pbo2-test --soc-llc 5 -o soc5\n  asrock_profile set  pbo2-test --byte 0x1B1=1 --dry-run\n  asrock_profile detect r5.bin --baseline r4.bin\n',
    # 符合 DDR4 的 2:1 关系 ✔
    '符合 DDR4 的 2:1 关系 ✔': 'matches the DDR4 2:1 ratio ✔',
    # 类型: U 盘配置档案（板型 %r / BIOS %r）
    '类型: U 盘配置档案（板型 %r / BIOS %r）': 'Type: USB config profile (board %r / BIOS %r)',
    # 类型: 整片 SPI 镜像
    '类型: 整片 SPI 镜像': 'Type: full SPI image',
    # 编号超出范围
    '编号超出范围': 'Number out of range',
    # 自动找到这些档案（新的在前）：
    '自动找到这些档案（新的在前）：': 'Found these profiles (newest first):',
    # 自动探测命中 %d 个候选；选用偏移最小的：长度前缀 @0x
    '自动探测命中 %d 个候选；选用偏移最小的：长度前缀 @0x%X、size=0x%X、小字节比例 %.0f%%': 'Auto-detection found %d candidate(s); using the lowest-addressed one: length prefix @0x%X, size=0x%X, small-byte ratio %.0f%%',
    # 若 info 显示的 Setup 变量位置不是 0x59、或
    '若 info 显示的 Setup 变量位置不是 0x59、或自动探测失败，': 'If `info` reports a Setup variable offset other than 0x59, or if auto-detection fails,',
    # 要写入的 Setup 原始数据（大小必须完全一致）
    '要写入的 Setup 原始数据（大小必须完全一致）': 'Raw Setup data to write (the size must match exactly)',
    # 要设成几级 (0-5):
    '要设成几级 (0-5): ': 'Level (0-5): ',
    # 覆盖原档案（危险）
    '覆盖原档案（危险）': 'OVERWRITE the source profile (dangerous)',
    # 请用 --setup-offset / --setup-si
    '请用 --setup-offset / --setup-size 手工指定。': 'specify it by hand with --setup-offset / --setup-size.',
    # 请选档案编号，或粘贴完整路径（q 退出）:
    '请选档案编号，或粘贴完整路径（q 退出）: ': 'Pick a number, or paste a full path (q to quit): ',
    # 读取单个字段
    '读取单个字段': 'Read a single field',
    # 读取失败: %s
    '读取失败: %s': 'Read failed: %s',
    # 输入不是数字: %s
    '输入不是数字: %s': 'Not a number: %s',
    # 输出文件
    '输出文件': 'Output file',
    # 输出文件（默认 <输入>.mod）
    '输出文件（默认 <输入>.mod）': 'Output file (default <input>.mod)',
    # 输出路径与输入相同。要覆盖原文件请显式加 --inplace
    '输出路径与输入相同。要覆盖原文件请显式加 --inplace（建议先备份）': 'Output path equals the input path. To overwrite in place, pass --inplace explicitly (back up first)',
    # 退出
    '退出': 'Exit',
    # 选操作编号:
    '选操作编号: ': 'Choose an action: ',
    # 链头(最旧)
    '链头(最旧)': 'head(oldest)',
    # 链尾(最新)
    '链尾(最新)': 'tail(newest)',
    # 链长
    '链长': 'chain',
    # 错误:
    '错误: ': 'Error: ',
    # 长度前缀位置: 0x%X (%d)
    '长度前缀位置: 0x%X (%d)': 'Length-prefix offset: 0x%X (%d)',
    # 顺便跟一份配置档案比对（核对载入的值是否真的生效）
    '顺便跟一份配置档案比对（核对载入的值是否真的生效）': 'Also compare against a config profile (to check whether the loaded value really took effect)',
    # 顺带把 Setup 原始数据导出到 OUT
    '顺带把 Setup 原始数据导出到 OUT': 'Also export the raw Setup data to OUT',
    # 高区 [10]
    '高区 [10]': 'high region [10]',
    # （可用 --setup-offset 0x%X --setu
    '（可用 --setup-offset 0x%X --setup-size 0x%X 把它塞回另一个档案）': '(use --setup-offset 0x%X --setup-size 0x%X to inject it into another profile)',
    # （用 DRAM Voltage 与 VTT_DDR 的 2:
    '（用 DRAM Voltage 与 VTT_DDR 的 2:1 关系判断偏移表是否还对得上）。': '(using the DRAM Voltage vs VTT_DDR 2:1 ratio to tell whether the offset table still lines up).',
    # ：尺寸不是常见的 0x280，请用 info 核对字段值是否
    '：尺寸不是常见的 0x280，请用 info 核对字段值是否合理': ': size is not the usual 0x280 — check the field values with `info`',
}



if __name__ == "__main__":
    sys.exit(main())
