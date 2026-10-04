#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
asrock_profile.py — ASRock BIOS 用户配置档案（U 盘档案）编辑器  [CLI + GUI 二合一]

在 ASRock 主板上，BIOS 提供 "Save User Default to USB flash drive" /
"Load User Default from USB flash drive"。导出的档案里嵌着一整份
UEFI Setup 变量的原样副本；载入档案时由固件自己把它写回活体变量。

本工具就是改这份副本里的指定字节 —— 用来修改那些「BIOS 菜单里没有条目、
但固件/驱动会读取」的设置（例如 VDDCR_SOC / CPU Load-Line Calibration）。

⚠️ 重要：本工具**不会**让 BIOS 菜单里多出条目。菜单里有没有某个选项由
       Setup 模块的 IFR 决定，跟本档案无关。改档只能改「值」，不能加「条目」。

启动方式
--------
    无参数 / 双击 / --gui     → 图形界面
    带子命令（info/set/...）   → 命令行模式

用法见 README.md，或 `asrock_profile -h`。

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


#: 各平台优先使用的界面字体。
#: ⚠ 这个界面**全是中文** —— 如果挑不到带 CJK 字形的字体，Linux 上会整片显示成
#:   豆腐块（□□□）。Tk 对不存在的字体名是"静默回退"，不会报错，所以必须
#:   拿 tkinter.font.families() 逐个核对，不能直接写死名字。
UI_FONT_PREFS = {
    "win32":  ["Microsoft YaHei UI", "Microsoft YaHei", "SimHei", "SimSun", "Segoe UI"],
    "darwin": ["PingFang SC", "Hiragino Sans GB", "Heiti SC", "STHeiti", "Helvetica Neue"],
    "linux":  ["Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Micro Hei",
               "WenQuanYi Zen Hei", "Droid Sans Fallback", "DejaVu Sans"],
}

#: 等宽字体（用于信息面板 / 日志 / 表格里的十六进制）
MONO_FONT_PREFS = {
    "win32":  ["Consolas", "Cascadia Mono", "Courier New"],
    "darwin": ["Menlo", "Monaco", "Courier New"],
    "linux":  ["DejaVu Sans Mono", "Liberation Mono", "Noto Sans Mono", "Courier New"],
}


def plat_key() -> str:
    """把 sys.platform 归成三档（其余一律当 linux 处理，含 BSD）。"""
    if sys.platform == "win32":
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def pick_font(available, prefs, fallback):
    """从候选里挑第一个系统真有的字体。"""
    low = {str(f).lower() for f in available}
    for name in prefs:
        if name.lower() in low:
            return name
    return fallback


def screen_size(root):
    """取屏幕尺寸；个别环境下 winfo_* 会抛异常，兜一层。"""
    try:
        return int(root.winfo_screenwidth()), int(root.winfo_screenheight())
    except Exception:
        return 1280, 800

# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #

def gui_available() -> bool:
    try:
        import tkinter                                     # noqa: F401
        return True
    except Exception:
        return False


def _dpi_aware():
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def _hide_console():
    """把控制台窗口藏起来（备用，默认不用）。

    打包成控制台子系统 exe 后，双击 GUI 会带一个黑框 —— 这是刻意的：
    CLI 模式必须有 stdout，而一个 exe 只能有一个子系统。
    真想要无黑框的话，在 `run_gui(hide_console=True)` 里调它即可。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)       # SW_HIDE
    except Exception:
        pass


class ProfileGUI:
    """图形界面。全部业务逻辑都复用上面的 Profile / build_plan / _report_and_write。"""

    # 「不改」的哨兵文本走模块级 no_change_text()：类体是导入时求值的，
    # 写成类属性会把语言固化在启动那一刻。

    def __init__(self, root, initial=None):
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox
        self.tk = tk
        self.ttk = ttk
        self.filedialog = filedialog
        self.messagebox = messagebox

        self.root = root
        self.path = None
        self.p = None
        self.pending = []          # [(offset, value), ...]


        # ---- 主题：win=vista / mac=aqua / 其它=clam ----
        self.C_OK = "#1B7F3B"
        self.C_WARN = "#B00020"
        style = ttk.Style()
        try:
            style.theme_use({"win32": "vista", "darwin": "aqua",
                             "linux": "clam"}[plat_key()])
        except Exception:
            pass
        style.configure("Warn.TLabel", foreground=self.C_WARN)
        style.configure("Ok.TLabel", foreground=self.C_OK)

        # ---- 字体：必须挑到带 CJK 字形的，否则 Linux 上整片豆腐块 ----
        from tkinter import font as tkfont
        try:
            fams = set(tkfont.families(root))
        except Exception:
            fams = set()
        ui_font = pick_font(fams, UI_FONT_PREFS[plat_key()], None)
        mono_font = pick_font(fams, MONO_FONT_PREFS[plat_key()], None)
        self.f_mono = (mono_font, 9) if mono_font else "TkFixedFont"
        if ui_font:
            try:
                style.configure(".", font=(ui_font, 10))
            except Exception:
                pass

        # ---- 窗口尺寸：不能超出屏幕（小屏笔记本 + HiDPI 缩放下很容易超） ----
        sw, sh = screen_size(root)
        w = min(980, max(640, sw - 100))
        h = min(940, max(520, sh - 140))
        root.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2), max(0, (sh - h) // 3)))
        root.minsize(min(820, w), min(680, h))

        self.outer = None
        self.baseline_path = None
        self._build_all(initial)

    # -- 构建 / 重建（切语言时整棵控件树重建） -------------------------------- #

    def _build_all(self, initial=None):
        """建出全部控件。切换语言时也是走这里（先 destroy 再重来）。"""
        ttk = self.ttk
        self.root.title(T(T("ASRock 配置档案编辑器  ·  asrock_profile %s")) % __version__)
        self.outer = ttk.Frame(self.root, padding=10)
        self.outer.pack(fill="both", expand=True)

        self._build_lang_row(self.outer)
        self._build_file_box(self.outer)
        self._build_info_box(self.outer)
        self._build_detect_box(self.outer)
        self._build_field_box(self.outer)
        self._build_edit_box(self.outer)
        self._build_output_box(self.outer)
        self._build_log_box(self.outer)

        self._refresh_all()
        if initial:
            self.load_file(initial)

    def _build_lang_row(self, parent):
        tk, ttk = self.tk, self.ttk
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(0, 6))
        ttk.Label(row, text=T(T("界面语言"))).pack(side="left")
        self.v_lang = tk.StringVar(value=lang_name(get_lang()))
        cb = ttk.Combobox(row, textvariable=self.v_lang, state="readonly",
                          width=9, values=[lang_name(c) for c in LANGS])
        cb.pack(side="left", padx=(6, 0))
        cb.bind("<<ComboboxSelected>>", self.on_lang_change)
        ttk.Label(row, text=T(T("切换后界面立刻重建，已载入的档案、待改动与日志都会保留"))
                  ).pack(side="left", padx=(10, 0))

    def on_lang_change(self, _evt=None):
        want = LANG_EN if self.v_lang.get() == "English" else LANG_ZH
        if want == get_lang():
            return
        set_lang(want)
        self.rebuild()

    def rebuild(self):
        """重建界面（换语言用）。

        Tk 没有「统一重设所有控件文本」的入口，逐个改反而容易漏；
        直接销毁控件树重建、再把状态灌回去，最省心也最不容易出错。
        """
        st = {
            "path": self.v_path.get(),
            "spi": self.v_spi.get(),
            "out": self.v_out.get(),
            "inplace": bool(self.v_inplace.get()),
            "dryrun": bool(self.v_dryrun.get()),
            "off": self.v_off.get(),
            "val": self.v_val.get(),
            "llc": self._llc_num(self.v_llc.get()),
            "soc": self._llc_num(self.v_socllc.get()),
            "pending": list(self.pending),
            "baseline": self.baseline_path,
            "detected": bool(self.lbl_detect.cget("text")),
        }
        self.outer.destroy()
        self._build_all()

        self.baseline_path = st["baseline"]
        self.pending = st["pending"]
        self.v_path.set(st["path"])
        self.v_spi.set(st["spi"])
        self.v_out.set(st["out"])
        self.v_inplace.set(st["inplace"])
        self.v_dryrun.set(st["dryrun"])
        self.v_off.set(st["off"])
        self.v_val.set(st["val"])
        self._set_llc(self.v_llc, st["llc"])
        self._set_llc(self.v_socllc, st["soc"])

        # 日志与检测结论都是「用旧语言渲染好的**文本**」，原样搬过来必然中英混排
        # ⇒ 不复用文本，改按语义重放：载入 → 待改动 → 检测。
        # 代价：切换前日志里的其它行（比如"已写出 xxx"）不会回来 —— 日志本来就
        # 是即时视图不是档案，这样换语言的观感更干净。
        self.log_clear()
        if st["path"] and os.path.exists(st["path"]):
            self.load_file(st["path"])          # 会重写「载入 + 板型警告」（新语言）
            self.pending = st["pending"]
        for off, val in self.pending:
            self.log(T("加入改动：Setup+0x%03X %s = %s")
                     % (off, self.p.describe_field(off) if self.p else "",
                        _fmt_value(off, val)))
        self._refresh_all()
        if st["detected"] and self.v_spi.get().strip():
            self.on_detect()                    # 重跑检测：日志与结论一起换语言

    def _set_llc(self, var, num):
        """把下拉框设成档位 num（None = 哨兵）。"""
        var.set(no_change_text() if num is None
                else "%s (%d)" % (LLC_OPTIONS[num], num))

    # -- 布局 --------------------------------------------------------------- #

    def _build_file_box(self, parent):
        tk, ttk = self.tk, self.ttk
        box = ttk.LabelFrame(parent, text=T(" 1. 档案 "), padding=8)
        box.pack(fill="x")
        self.v_path = tk.StringVar()
        e = ttk.Entry(box, textvariable=self.v_path)
        e.pack(side="left", fill="x", expand=True)
        ttk.Button(box, text=T("打开档案…"), command=self.on_browse).pack(side="left", padx=(6, 0))
        ttk.Button(box, text=T("重新载入"), command=self.on_reload).pack(side="left", padx=(6, 0))

    def _build_info_box(self, parent):
        tk, ttk = self.tk, self.ttk
        box = ttk.LabelFrame(parent, text=T(" 2. 档案信息与检查 "), padding=8)
        box.pack(fill="x", pady=(8, 0))
        self.txt_info = tk.Text(box, height=7, wrap="none", state="disabled",
                                font=self.f_mono)
        self.txt_info.pack(fill="x")
        # 用 tk.Label 而不是 ttk.Label：macOS 的 aqua 主题会忽略 ttk 的 foreground，
        # 红字告警会变成黑色。经典 Label 在所有平台都老老实实听 fg=。
        self.lbl_warn = tk.Label(box, text="", fg=self.C_WARN, anchor="w",
                                 justify="left", wraplength=860)
        self.lbl_warn.pack(fill="x", pady=(6, 0))

    def _build_detect_box(self, parent):
        tk, ttk = self.tk, self.ttk
        box = ttk.LabelFrame(
            parent,
            text=T(" 3. 检测当前状态（读整片 SPI 镜像，例如 AFU 备份出来的 r5.bin）"),
            padding=8)
        box.pack(fill="x", pady=(8, 0))

        row = ttk.Frame(box)
        row.pack(fill="x")
        self.v_spi = tk.StringVar()
        ttk.Entry(row, textvariable=self.v_spi).pack(side="left", fill="x",
                                                     expand=True)
        ttk.Button(row, text=T("选择镜像…"), command=self.on_pick_spi).pack(
            side="left", padx=(6, 0))
        ttk.Button(row, text=T("检测"), command=self.on_detect).pack(
            side="left", padx=(6, 0))
        ttk.Button(row, text=T("用作基线"), command=self.on_use_baseline).pack(
            side="left", padx=(6, 0))

        self.lbl_detect = tk.Label(box, text=T("（未检测）"), anchor="w",
                                   justify="left", wraplength=880)
        self.lbl_detect.pack(fill="x", pady=(6, 0))
        # baseline_path 由 __init__ 初始化；重建界面时不能在这里清掉。

    # -- 检测 --------------------------------------------------------------- #

    def on_pick_spi(self):
        p = self.filedialog.askopenfilename(
            title=T("选择整片 SPI 镜像（AFU 的 /O 备份产物）"),
            filetypes=[(T("BIN 镜像"), "*.bin"), (T("所有文件"), "*.*")])
        if p:
            self.v_spi.set(p)

    def on_use_baseline(self):
        if not self.v_spi.get().strip():
            self.messagebox.showwarning(T("未选择镜像"), T("请先选择一个 SPI 镜像。"))
            return
        self.baseline_path = self.v_spi.get().strip()
        self.log(T("已把 %s 记为基线（下次检测会做基线对照，判定 100%% 确定）")
                 % self.baseline_path)

    def on_detect(self):
        path = self.v_spi.get().strip()
        if not path:
            self.messagebox.showwarning(
                T("未选择镜像"),
                T("请先选择一张整片 SPI 镜像（用 AFU 的 /O 备份出来的 .bin）。\n"
                "也可以直接选一份 U 盘配置档案，但那样只能看到档案里的值。"))
            return
        try:
            lines, verdict = detect_lines(path, self.baseline_path, self.path)
        except ProfileError as e:
            self.messagebox.showerror(T("检测失败"), str(e))
            self.log(T("✘ 检测失败: %s") % e)
            return
        except (OSError, ValueError) as e:
            self.messagebox.showerror(T("检测失败"), str(e))
            return

        self.log_clear()
        self.log("\n".join(lines))

        # 只看符号，不看词 —— 词是会被翻译的
        color = self.C_WARN if "✘" in verdict else self.C_OK
        self.lbl_detect.configure(text="⇒ " + verdict, fg=color)
        if self.p is not None:
            self.log(T("\n（上面同时与当前打开的档案 %s 做了比对）")
                     % os.path.basename(self.path))

    def _build_field_box(self, parent):
        ttk = self.ttk
        box = ttk.LabelFrame(parent, text=T(" 4. 已知字段当前值（只读，供核对偏移表是否匹配）"),
                             padding=8)
        box.pack(fill="both", expand=False, pady=(8, 0))
        cols = ("off", "name", "value")
        tv = ttk.Treeview(box, columns=cols, show="headings", height=6)
        tv.heading("off", text=T("Setup 偏移"))
        tv.heading("name", text=T("字段"))
        tv.heading("value", text=T("当前值"))
        tv.column("off", width=110, anchor="w", stretch=False)
        tv.column("name", width=430, anchor="w")
        tv.column("value", width=150, anchor="w", stretch=False)
        sb = ttk.Scrollbar(box, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree_fields = tv

    def _build_edit_box(self, parent):
        tk, ttk = self.tk, self.ttk
        box = ttk.LabelFrame(parent, text=T(" 5. 修改 "), padding=8)
        box.pack(fill="both", expand=False, pady=(8, 0))

        grid = ttk.Frame(box)
        grid.pack(fill="x")

        ttk.Label(grid, text="CPU Load-Line Calibration (Setup+0x190)").grid(
            row=0, column=0, sticky="w", pady=2)
        self.v_llc = tk.StringVar(value=no_change_text())
        self.cb_llc = ttk.Combobox(grid, textvariable=self.v_llc, state="readonly",
                                   width=18, values=self._llc_values())
        self.cb_llc.grid(row=0, column=1, sticky="w", padx=(10, 0), pady=2)

        ttk.Label(grid, text="VDDCR_SOC Load-Line Calibration (Setup+0x191)").grid(
            row=1, column=0, sticky="w", pady=2)
        self.v_socllc = tk.StringVar(value=no_change_text())
        ttk.Combobox(grid, textvariable=self.v_socllc, state="readonly",
                     width=18, values=self._llc_values()).grid(
            row=1, column=1, sticky="w", padx=(10, 0), pady=2)

        ttk.Label(grid, text=T("自定义字节（偏移支持别名或十六进制，如 0x1B1）")).grid(
            row=2, column=0, sticky="w", pady=(8, 2))
        sub = ttk.Frame(grid)
        sub.grid(row=2, column=1, sticky="w", padx=(10, 0), pady=(8, 2))
        self.v_off = tk.StringVar()
        self.v_val = tk.StringVar()
        ttk.Entry(sub, textvariable=self.v_off, width=14).pack(side="left")
        ttk.Label(sub, text="=").pack(side="left", padx=3)
        ttk.Entry(sub, textvariable=self.v_val, width=8).pack(side="left")
        ttk.Button(sub, text=T("添加"), width=6, command=self.on_add_pending).pack(
            side="left", padx=(6, 0))
        ttk.Button(sub, text=T("删除选中"), width=9, command=self.on_del_pending).pack(
            side="left", padx=(6, 0))

        cols = ("off", "name", "old", "new")
        tv = ttk.Treeview(box, columns=cols, show="headings", height=5)
        for c, t, w in (("off", T("Setup 偏移"), 100), ("name", T("字段"), 320),
                        ("old", T("原值"), 90), ("new", T("新值"), 90)):
            tv.heading(c, text=t)
            tv.column(c, width=w, anchor="w", stretch=(c == "name"))
        tv.pack(fill="x", pady=(8, 0))
        self.tree_pending = tv

    def _build_output_box(self, parent):
        tk, ttk = self.tk, self.ttk
        box = ttk.LabelFrame(parent, text=T(" 6. 输出 "), padding=8)
        box.pack(fill="x", pady=(8, 0))
        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Label(row, text=T("输出文件")).pack(side="left")
        self.v_out = tk.StringVar()
        ttk.Entry(row, textvariable=self.v_out).pack(side="left", fill="x",
                                                     expand=True, padx=(6, 6))
        ttk.Button(row, text=T("另存为…"), command=self.on_pick_out).pack(side="left")

        row2 = ttk.Frame(box)
        row2.pack(fill="x", pady=(6, 0))
        self.v_inplace = tk.BooleanVar(value=False)
        self.v_dryrun = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2, text=T("覆盖原文件（建议先备份）"),
                        variable=self.v_inplace).pack(side="left")
        ttk.Checkbutton(row2, text=T("仅预览（不写文件）"),
                        variable=self.v_dryrun).pack(side="left", padx=(14, 0))

        row3 = ttk.Frame(box)
        row3.pack(fill="x", pady=(8, 0))
        self.btn_apply = ttk.Button(row3, text=T("应用并写出"), command=self.on_apply)
        self.btn_apply.pack(side="left")
        ttk.Button(row3, text=T("清空改动"), command=self.on_clear_pending).pack(
            side="left", padx=(6, 0))
        ttk.Button(row3, text=T("打开输出目录"), command=self.on_open_dir).pack(
            side="left", padx=(6, 0))

    def _build_log_box(self, parent):
        tk, ttk = self.tk, self.ttk
        box = ttk.LabelFrame(parent, text=T(" 7. 日志 "), padding=8)
        box.pack(fill="both", expand=True, pady=(8, 0))
        self.txt_log = tk.Text(box, height=9, wrap="none", state="disabled",
                               font=self.f_mono)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=sb.set)
        self.txt_log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    @staticmethod
    def _llc_values():
        return [no_change_text()] + ["%s (%d)" % (v, k) for k, v in sorted(LLC_OPTIONS.items())]

    # -- 日志 --------------------------------------------------------------- #

    def log(self, text=""):
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", text + "\n")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def log_clear(self):
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")

    def _set_info_text(self, text):
        self.txt_info.configure(state="normal")
        self.txt_info.delete("1.0", "end")
        self.txt_info.insert("1.0", text)
        self.txt_info.configure(state="disabled")

    # -- 载入 --------------------------------------------------------------- #

    def on_browse(self):
        p = self.filedialog.askopenfilename(
            title=T("选择 ASRock 配置档案"),
            filetypes=[(T("所有文件"), "*.*")])
        if p:
            self.load_file(p)

    def on_reload(self):
        if self.path:
            self.load_file(self.path)

    def load_file(self, path):
        try:
            prof = load(path)
        except ProfileError as e:
            self.messagebox.showerror(T("无法解析档案"), str(e))
            self.log(T("✘ 载入失败：%s") % e)
            return
        except FileNotFoundError:
            self.messagebox.showerror(T("找不到文件"), path)
            return
        except OSError as e:
            self.messagebox.showerror(T("读取失败"), str(e))
            return

        self.p = prof
        self.path = path
        self.v_path.set(path)
        if not self.v_out.get() or self.v_out.get().endswith(".mod"):
            self.v_out.set(path + ".mod")

        self.pending = []
        self._refresh_all()

        warns = board_warnings(prof)
        self.log(T("载入: %s（%d 字节，Setup @0x%X size=0x%X）")
                 % (os.path.abspath(path), len(prof.data), prof.setup_off, prof.setup_size))
        for lv, msg in warns:
            self.log(("⚠ " if lv == WARN else "✘ ") + msg.replace("\n", "\n  ").replace("    ", "  "))
        if not warns:
            self.log(T("✔ 板型 / 版本 / 字段自检：全部通过"))

    def _refresh_info(self):
        if not self.p:
            self._set_info_text("")
            # tk.Label 没有 -style 选项（那是 ttk 的），写它会抛 TclError
            self.lbl_warn.configure(text="", fg=self.C_OK)
            return
        lines = info_lines(self.p, self.path)
        self._set_info_text("\n".join(lines))

        warns = board_warnings(self.p)
        if not warns:
            self.lbl_warn.configure(text=T("✔ 板型、BIOS 版本与字段自检均通过。"),
                                    fg=self.C_OK)
        else:
            txt = "\n".join(("⚠ " if lv == WARN else "✘ ") + msg for lv, msg in warns)
            self.lbl_warn.configure(text=txt, fg=self.C_WARN)

    def _refresh_fields(self):
        tv = self.tree_fields
        tv.delete(*tv.get_children())
        if not self.p:
            return
        for off, size, kind, name in KNOWN_FIELDS:
            if off + size > self.p.setup_size:
                continue
            v = self.p.get_field(off, size)
            tv.insert("", "end", values=("0x%03X" % off, name,
                                         _fmt_value(off, v) if size == 1 else str(v)))

    def _refresh_pending(self):
        tv = self.tree_pending
        tv.delete(*tv.get_children())
        for off, val in self.pending:
            old = self.p.get_field(off, 1) if self.p else 0
            tv.insert("", "end", iid="0x%X" % off,
                      values=("0x%03X" % off, self.p.describe_field(off) if self.p else "",
                              _fmt_value(off, old), _fmt_value(off, val)))

    def _refresh_all(self):
        self._refresh_info()
        self._refresh_fields()
        self._refresh_pending()

    # -- 编辑 --------------------------------------------------------------- #

    @staticmethod
    def _llc_num(s):
        """从下拉框显示文本里取档位号；哨兵文本返回 None。

        刻意不拿「不改」那句译文来比较 —— 换了语言旧值就认不出来了。
        也不能假设「哨兵不含括号」：中文哨兵本身写作 `(不改)`，
        所以解析失败一律当哨兵处理，别让它抛出去。
        """
        if not s or "(" not in s:
            return None
        try:
            return int(s.rsplit("(", 1)[1].rstrip(")"))
        except ValueError:
            return None

    def on_add_pending(self):
        if not self.p:
            self.messagebox.showwarning(T("还没有载入档案"), T("请先打开一个配置档案。"))
            return
        spec = self.v_off.get().strip()
        raw = self.v_val.get().strip()
        if not spec or not raw:
            self.messagebox.showwarning(T("输入不完整"), T("请同时填写偏移和值。"))
            return
        try:
            off = _resolve_target(spec)
            val = int(raw, 0)
        except ProfileError as e:
            self.messagebox.showerror(T("无法识别"), str(e))
            return
        except ValueError:
            self.messagebox.showerror(T("值不合法"), T("值可以是十进制或 0x 开头的十六进制。"))
            return
        try:
            _check_off_spec(self.p, off)
        except ProfileError as e:
            self.messagebox.showerror(T("偏移越界"), str(e))
            return
        if not 0 <= val <= 0xFF:
            self.messagebox.showerror(T("值越界"), T("字节值必须在 0..255。"))
            return
        self.pending = [x for x in self.pending if x[0] != off]
        self.pending.append((off, val))
        self.v_off.set("")
        self.v_val.set("")
        self._refresh_pending()
        self.log(T("加入改动：Setup+0x%03X %s = %s")
                 % (off, self.p.describe_field(off), _fmt_value(off, val)))

    def on_del_pending(self):
        sel = self.tree_pending.selection()
        if not sel:
            return
        drop = {int(s, 16) for s in sel}
        self.pending = [x for x in self.pending if x[0] not in drop]
        self._refresh_pending()

    def on_clear_pending(self):
        self.pending = []
        self.v_llc.set(no_change_text())
        self.v_socllc.set(no_change_text())
        self._refresh_pending()
        self.log(T("已清空待改动列表"))

    def on_pick_out(self):
        p = self.filedialog.asksaveasfilename(
            title=T("输出档案另存为"),
            initialfile=os.path.basename(self.v_out.get()) or "profile.mod")
        if p:
            self.v_out.set(p)

    def on_open_dir(self):
        target = self.v_out.get() or self.path
        if not target:
            return
        d = os.path.dirname(os.path.abspath(target))
        try:
            if sys.platform == "win32":
                os.startfile(d)                            # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", d])              # 传 list，路径含空格也不用管
            else:
                subprocess.Popen(["xdg-open", d])
        except Exception as e:
            self.messagebox.showerror(T("打开失败"), str(e))

    # -- 应用 --------------------------------------------------------------- #

    def on_apply(self):
        if not self.p:
            self.messagebox.showwarning(T("还没有载入档案"), T("请先打开一个配置档案。"))
            return

        # 收集改动：下拉框 + 待改动列表
        try:
            llc = self._llc_num(self.v_llc.get())
            soc = self._llc_num(self.v_socllc.get())
        except Exception:
            self.messagebox.showerror(T("解析失败"), T("下拉框取值异常，请重新选择。"))
            return

        raw = list(self.pending)
        # 下拉框优先级高于列表里的同偏移项
        for off in (0x190, 0x191):
            raw = [x for x in raw if x[0] != off]
        if llc is not None:
            raw.append((0x190, llc))
        if soc is not None:
            raw.append((0x191, soc))

        if not raw:
            self.messagebox.showinfo(T("没有改动"), T("没有指定任何要修改的字段。"))
            return

        # 重新解析一份干净副本，避免上次失败残留
        try:
            prof = load(self.path)
        except Exception as e:
            self.messagebox.showerror(T("重新载入失败"), str(e))
            return

        # 板型警告 → 二次确认
        warns = board_warnings(prof)
        if warns:
            txt = "\n\n".join(msg for lv, msg in warns)
            if not self.messagebox.askyesno(
                    T("⚠ 版型检查未通过"),
                    txt + T("\n\n仍然要继续吗？\n（建议先取消，把 info 输出贴到 issue）"),
                    icon="warning", default="no"):
                self.log(T("用户取消了操作（版型警告）。"))
                return

        try:
            plan = build_plan(prof, llc, soc, raw)
            diffs = apply_plan(prof, plan, quiet=True)
        except ProfileError as e:
            self.messagebox.showerror(T("无法构造改动"), str(e))
            return

        if not diffs:
            self.messagebox.showinfo(T("没有改动"), T("指定的值与当前值相同，无需写出。"))
            self.log(T("没有实际改动。"))
            return

        args = argparse.Namespace(
            file=self.path,
            output=self.v_out.get() or None,
            inplace=bool(self.v_inplace.get()),
            dry_run=bool(self.v_dryrun.get()),
        )

        # 复用命令行那套报告/写出/自检逻辑，把输出抓进日志窗口
        set_color(False)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                out = _report_and_write(prof, args, diffs)
        except (ProfileError, OSError) as e:
            self.log_clear()
            self.log("✘ " + str(e))
            self.messagebox.showerror(T("写出失败"), str(e))
            return
        finally:
            set_color(sys.stdout.isatty())

        self.log_clear()
        self.log(buf.getvalue().rstrip())
        if args.dry_run:
            self.log(T("\n（仅预览：未写出文件）"))
        else:
            self.log_clear()
            self.log(buf.getvalue().rstrip())
            self.messagebox.showinfo(
                T("完成"),
                T("已写出：\n%s\n\n请把它拷到 FAT32 U 盘根目录，"
                "再进 BIOS 用 “Load User Default from USB flash drive” 载入。")
                % os.path.abspath(out))
            # 写出后把界面同步到输出文件，方便连续做多档位
            if os.path.abspath(out) != os.path.abspath(self.path):
                self.load_file(out)
                self.log(T("界面已切换到新写出档案：%s") % out)


def create_gui(root, initial=None):
    """构建 GUI 并返回控制器（不进入 mainloop）。便于自动化测试。"""
    return ProfileGUI(root, initial)


def run_gui(paths=None, hide_console=False):
    if not gui_available():
        sys.stderr.write(T("错误: 无法加载 GUI（本机 Python 没带 tkinter）。\n"))
        sys.stderr.write(T("请改用命令行模式，例如: %s info <档案>\n")
                         % os.path.basename(sys.argv[0]))
        return 1
    import tkinter as tk
    _dpi_aware()
    if hide_console:
        _hide_console()
    set_color(False)
    root = tk.Tk()
    create_gui(root, paths[0] if paths else None)
    root.mainloop()
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
                    "用来修改档案里内嵌的 Setup 变量字节。"
                    "  不带参数运行即打开图形界面。"),
        epilog=T("示例:\n"
               "  asrock_profile                              # 打开图形界面\n"
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

    p = sub.add_parser("gui", help=T("打开图形界面（可跟一个档案路径）"))
    p.add_argument("files", nargs="*", help=T("启动时直接打开的档案（可选）"))
    p.set_defaults(func=None)

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


GUI_FLAGS = ("gui", "--gui", "-g", "/gui")
SUBCOMMANDS = ("gui", "info", "get", "set", "dump", "inject", "boards")


def main(argv=None):
    argv = list(sys.argv[1:]) if argv is None else list(argv)

    # 先定语言再建 parser：argparse 的 help 是**建 parser 时**取译文的，
    # 顺序反了的话 -h 永远显示中文。
    argv = _apply_lang(argv)
    encoding_hint()

    # 显式 GUI 入口：gui [file]
    if argv and argv[0].lower() in GUI_FLAGS:
        return run_gui([a for a in argv[1:] if not a.startswith("-")])

    # 把档案直接拖到 exe 上（argv[0] 是存在的文件路径，不是子命令）⇒ 也用 GUI 打开。
    # 只对「确实存在的文件」生效，所以子命令打错字时仍会走 argparse 给出正确报错。
    if (argv and not argv[0].startswith("-")
            and argv[0] not in SUBCOMMANDS and os.path.exists(argv[0])
            and gui_available()):
        return run_gui([argv[0]])

    # 无参数：GUI。控制台子系统下黑框会一起出现，正好当日志用，不藏。
    if not argv:
        if gui_available():
            return run_gui(hide_console=False)
        build_parser().print_help()
        return 2

    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "cmd", None):
        ap.print_help()
        return 2
    if args.cmd == "gui":
        return run_gui(args.files)

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
        return 130



# --------------------------------------------------------------------------- #
# i18n 译文表（key = 中文原文，value = 英文）
# --------------------------------------------------------------------------- #
#   自动生成 / 人工逐条校对。`T()` 只做一次 dict 查询（O(1)），
#   查不到就返回原文 ⇒ 漏译最多显示成中文，不会崩。
#   改文案时的规矩：改了中文 key，就要同步改这里的 key（否则那条会退回中文显示）。
EN = {
    # 仍然要继续吗？
    '\n\n仍然要继续吗？\n（建议先取消，把 info 输出贴到 issue）': (
        ''
        '\n'
        '\nContinue anyway?'
        '\n(Recommended: cancel and paste the `info` output into an issue)'
    ),

    # 其余候选：
    '\n        其余候选：': (
        ''
        '\n        other candidates: '
    ),

    # 已导出 Setup 原始数据 → %s
    '\n已导出 Setup 原始数据 → %s': (
        ''
        '\nExported raw Setup data → %s'
    ),

    # 没有任何改动。
    '\n没有任何改动。': (
        ''
        '\nNo changes.'
    ),

    # （上面同时与当前打开的档案 %s 做了比对）
    '\n（上面同时与当前打开的档案 %s 做了比对）': (
        ''
        '\n(also compared against the profile currently open: %s)'
    ),

    # （仅预览：未写出文件）
    '\n（仅预览：未写出文件）': (
        ''
        '\n(preview only: no file written)'
    ),

    # ← ⚠ 未收录的版本
    '   ← ⚠ 未收录的版本': '   ← ⚠ version not in the known list',

    # ⚠ 单张镜像属**推断**（证据充分但非证明）。想 100%
    '   ⚠ 单张镜像属**推断**（证据充分但非证明）。想 100% 确定，加 --baseline 给上一次的镜像。': (
        '   ⚠ A single image only gives an **inference** (strong evidence, not proof). For'
        ' 100% certainty, add --baseline with the previous image.'
    ),

    # 判定依据：**基线对照** —— 基线里没有、当前镜像里有，
    '   判定依据：**基线对照** —— 基线里没有、当前镜像里有，且是新增副本中偏移最大的': (
        '   Basis: **baseline comparison** — absent in the baseline, present now, and the'
        ' highest-addressed of the new copies'
    ),

    # 判定依据：活跃链的链尾（该链尾内容在其它链里找不到 ⇒ 是全
    '   判定依据：活跃链的链尾（该链尾内容在其它链里找不到 ⇒ 是全新状态；且链内变体数最多 ⇒ 记录着每次状态变化）': (
        '   Basis: the tail of the live chain (its content appears in no other chain ⇒ it is a'
        ' brand-new state; and it holds the most variants ⇒ it records every state change)'
    ),

    # 要检测当前生效值，请给一张整片 SPI 镜像（AFU 备份出
    '   要检测当前生效值，请给一张整片 SPI 镜像（AFU 备份出来的 .bin）。': (
        '   To detect the active value, pass a full SPI image (a .bin backed up with AFU).'
    ),

    # %-8s %-46s  已实测版本: %s
    '  %-8s %-46s  已实测版本: %s': '  %-8s %-46s  tested versions: %s',

    # 1) 把 %s 拷到 FAT32 U 盘根目录
    '  1) 把 %s 拷到 FAT32 U 盘根目录': '  1) Copy %s to the root of a FAT32 USB stick',

    # 2) 进 BIOS → Load User Default 
    '  2) 进 BIOS → Load User Default from USB flash drive → 选它': (
        '  2) Enter BIOS → Load User Default from USB flash drive → pick it'
    ),

    # 3) F10 保存退出 → 重启
    '  3) F10 保存退出 → 重启': '  3) F10 to save and exit → reboot',

    # ← 活跃链
    '  ← 活跃链': '  ← live chain',

    # ⇒ 最新写入的副本 = @0x%06X（新增副本里偏移最大的
    '  ⇒ 最新写入的副本 = @0x%06X（新增副本里偏移最大的那个）': (
        '  ⇒ newest copy written = @0x%06X (the highest-addressed of the new copies)'
    ),

    # ⚠ BIOS 菜单里不会因此多出条目；换档位就换一个档案载入
    '  ⚠ BIOS 菜单里不会因此多出条目；换档位就换一个档案载入': (
        '  ⚠ This will NOT add a BIOS menu entry; to change level, load a different profile'
    ),

    # ⚠ 副本数远多于预期是正常的（NVRAM compactio
    '  ⚠ 副本数远多于预期是正常的（NVRAM compaction 会留下大量历史版本），关键是**哪一条最新**': (
        '  ⚠ Far more copies than expected is normal (NVRAM compaction leaves plenty of'
        ' historical versions); what matters is **which one is newest**'
    ),

    # ⚠ 没有新增副本 —— 基线可能不是同一个状态，退回单张镜像
    '  ⚠ 没有新增副本 —— 基线可能不是同一个状态，退回单张镜像的推断': (
        '  ⚠ No new copies — the baseline may not be the same state; falling back to'
        ' single-image inference'
    ),

    # ✘ 一个副本都没找到 —— 这张镜像可能不是本板/本版本的。
    '  ✘ 一个副本都没找到 —— 这张镜像可能不是本板/本版本的。': (
        '  ✘ Not a single copy found — this image is probably not from this board/version.'
    ),

    # 两次读取之间：新增 %d 处、消失 %d 处（新增 = 这段
    '  两次读取之间：新增 %d 处、消失 %d 处（新增 = 这段时间里写进去的）': (
        '  Between the two reads: %d new, %d gone (new = written in that window)'
    ),

    # 交叉核对: DRAM %d mV / VTT_DDR %d 
    '  交叉核对: DRAM %d mV / VTT_DDR %d mV ⇒ %s': '  Cross-check: DRAM %d mV / VTT_DDR %d mV ⇒ %s',

    # 基线副本 %d 处，当前副本 %d 处
    '  基线副本 %d 处，当前副本 %d 处': '  baseline copies: %d, current copies: %d',

    # 文件 0x%06X  %02X → %02X  Setup+
    '  文件 0x%06X  %02X → %02X  Setup+0x%03X  %s': (
        '  file 0x%06X  %02X → %02X  Setup+0x%03X  %s'
    ),

    # 文件 0x%06X  %02X → %02X%s  %s
    '  文件 0x%06X  %02X → %02X%s  %s': '  file 0x%06X  %02X → %02X%s  %s',

    # 新增副本的 +0x190 分布: %s
    '  新增副本的 +0x190 分布: %s': '  +0x190 distribution of the new copies: %s',

    # 档案里的 CPU LLC = %s；镜像里的 = %s ⇒ 
    '  档案里的 CPU LLC = %s；镜像里的 = %s ⇒ %s': (
        '  CPU LLC in the profile = %s; in the image = %s ⇒ %s'
    ),

    # 特征串命中 %d 处；与共识副本比对后保留 %d 处（差异 
    '  特征串命中 %d 处；与共识副本比对后保留 %d 处（差异 %d–%d 字节）、淘汰 %d 处': (
        '  signature hits: %d; after comparison with the consensus copy, kept %d (differences'
        ' %d–%d bytes), rejected %d'
    ),

    # 1. 档案
    ' 1. 档案 ': ' 1. Profile ',

    # 2. 档案信息与检查
    ' 2. 档案信息与检查 ': ' 2. Profile info & checks ',

    # 3. 检测当前状态（读整片 SPI 镜像，例如 AFU 备份
    ' 3. 检测当前状态（读整片 SPI 镜像，例如 AFU 备份出来的 r5.bin）': (
        ' 3. Detect current state (read a full SPI image, e.g. r5.bin backed up by AFU)'
    ),

    # 4. 已知字段当前值（只读，供核对偏移表是否匹配）
    ' 4. 已知字段当前值（只读，供核对偏移表是否匹配）': (
        ' 4. Known field values (read-only; use it to check the offset table)'
    ),

    # 5. 修改
    ' 5. 修改 ': ' 5. Changes ',

    # 6. 输出
    ' 6. 输出 ': ' 6. Output ',

    # 7. 日志
    ' 7. 日志 ': ' 7. Log ',

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
    '**不符合**，这条链可能不是真 Setup 副本 ✘': (
        '**does NOT match**, this chain may not be a real Setup copy ✘'
    ),

    # --dry-run：仅预览，未写出任何文件（以上为将会发生的
    '--dry-run：仅预览，未写出任何文件（以上为将会发生的改动）。': (
        '--dry-run: preview only, nothing was written (the above is what would happen).'
    ),

    # === Setup 副本定位（内容特征串全片扫描） ===
    '=== Setup 副本定位（内容特征串全片扫描） ===': (
        '=== Locating Setup copies (whole-image scan for the content signature) ==='
    ),

    # === 与基线对照: %s ===
    '=== 与基线对照: %s ===': '=== Compared with the baseline: %s ===',

    # === 与档案 %s 比对 ===
    '=== 与档案 %s 比对 ===': '=== Compared with profile %s ===',

    # === 副本链表（按 NVAR 头的 u24 指针串成；链尾
    '=== 副本链表（按 NVAR 头的 u24 指针串成；链尾 = 最新写入） ===': (
        '=== Copy chains (linked by the u24 pointer in each NVAR header; tail = newest write)'
        ' ==='
    ),

    # === 档案里写明的值 ===
    '=== 档案里写明的值 ===': '=== Value as stored in the profile ===',

    # === 结论（读活跃链的链尾记录 @0x%06X） ===
    '=== 结论（读活跃链的链尾记录 @0x%06X） ===': (
        "=== Verdict (reading the live chain's tail record @0x%06X) ==="
    ),

    # ASRock BIOS 用户配置档案（U 盘档案）编辑器 —
    'ASRock BIOS 用户配置档案（U 盘档案）编辑器 —— 用来修改档案里内嵌的 Setup 变量字节。  不带参数运行即打开图形界面。': (
        'ASRock BIOS profile (USB config file) editor — for editing the Setup variable bytes'
        ' embedded in such a profile.  Run it with no arguments to open the GUI.'
    ),

    # ASRock 配置档案编辑器  ·  asrock_prof
    'ASRock 配置档案编辑器  ·  asrock_profile %s': 'ASRock profile editor  ·  asrock_profile %s',

    # ASRock 配置档案（BIOS 导出到 U 盘的那个文件）
    'ASRock 配置档案（BIOS 导出到 U 盘的那个文件）': (
        'ASRock profile (the file the BIOS exports to a USB stick)'
    ),

    # BIN 镜像
    'BIN 镜像': 'BIN images',

    # BIOS 版本: %r%s
    'BIOS 版本: %r%s': 'BIOS version: %r%s',

    # BIOS 版本未收录：档案版本是 %r，已实测的是 %s。
    'BIOS 版本未收录：档案版本是 %r，已实测的是 %s。\n    同板型换版本通常兼容，但 Setup 变量布局有变动的可能，改前请核对字段值是否合理。': (
        'BIOS version not in the known list: the profile says %r, tested: %s.'
        '\n    A different version of the same board is usually compatible, but the Setup'
        ' variable layout may have moved — check the field values first.'
    ),

    # CPU Load-Line Calibration（Setu
    'CPU Load-Line Calibration（Setup+0x190），0=Auto 1..5=Level': (
        'CPU Load-Line Calibration (Setup+0x190), 0=Auto 1..5=Level'
    ),

    # Load-Line 等级只能是 0..5（0=Auto），给
    'Load-Line 等级只能是 0..5（0=Auto），给的是 %d': 'Load-Line level must be 0..5 (0=Auto), got %d',

    # Setup 偏移
    'Setup 偏移': 'Setup offset',

    # Setup 原始数据（%d 字节）→ %s
    'Setup 原始数据（%d 字节）→ %s': 'Raw Setup data (%d bytes) → %s',

    # Setup 变量位置: 0x%X–0x%X  (%d 字节)
    'Setup 变量位置: 0x%X–0x%X  (%d 字节)': 'Setup variable: 0x%X–0x%X  (%d bytes)',

    # Setup 变量位置: 0x%X–0x%X (%d 字节)
    'Setup 变量位置: 0x%X–0x%X (%d 字节)': 'Setup variable: 0x%X–0x%X (%d bytes)',

    # Setup 变量大小（默认 0x280）
    'Setup 变量大小（默认 0x280）': 'Setup variable size (default 0x280)',

    # Setup 块 [0x%X, 0x%X) 超出文件范围（文件
    'Setup 块 [0x%X, 0x%X) 超出文件范围（文件 %d 字节）': (
        'Setup block [0x%X, 0x%X) is outside the file (%d bytes)'
    ),

    # Setup+0x%03X 未生效
    'Setup+0x%03X 未生效': 'Setup+0x%03X did not take effect',

    # VDDCR_SOC Load-Line Calibratio
    'VDDCR_SOC Load-Line Calibration（Setup+0x191）': (
        'VDDCR_SOC Load-Line Calibration (Setup+0x191)'
    ),

    # [版型警告]
    '[版型警告]': '[board warning]',

    # [跳过]
    '[跳过]': '[skip]',

    # [错误]
    '[错误]': '[error]',

    # ⚠ 版型检查未通过
    '⚠ 版型检查未通过': '⚠ Board check failed',

    # ⚠ 读档案 %s 失败: %s
    '⚠ 读档案 %s 失败: %s': '⚠ Failed to read profile %s: %s',

    # ⚠ 这是**档案里的值**，不是主板上当前生效的值。
    '⚠ 这是**档案里的值**，不是主板上当前生效的值。': (
        '⚠ This is the value **inside the profile**, not what is currently active on the'
        ' board.'
    ),

    # ✔ 板型 / 版本 / 字段自检：全部通过
    '✔ 板型 / 版本 / 字段自检：全部通过': '✔ Board / version / field self-check: all passed',

    # ✔ 板型、BIOS 版本与字段自检均通过。
    '✔ 板型、BIOS 版本与字段自检均通过。': '✔ Board, BIOS version and field self-check all passed.',

    # ✔ 自检通过：长度一致，且只有上述字节被改动
    '✔ 自检通过：长度一致，且只有上述字节被改动': (
        '✔ Self-check passed: same length, and only the bytes listed above were changed'
    ),

    # ✘ 检测失败: %s
    '✘ 检测失败: %s': '✘ Detection failed: %s',

    # ✘ 自检失败:
    '✘ 自检失败:': '✘ Self-check failed:',

    # ✘ 载入失败：%s
    '✘ 载入失败：%s': '✘ Load failed: %s',

    # 、
    '、': ', ',

    # 一致
    '一致': 'same',

    # 一致 ✔
    '一致 ✔': 'match ✔',

    # 上一次读的镜像 —— 给了就能 100%% 确定（靠「哪条链
    '上一次读的镜像 —— 给了就能 100%% 确定（靠「哪条链增长了」判定）': (
        'The previously read image — supplying it makes the verdict 100%% certain (decided by'
        ' which chain grew)'
    ),

    # 下一步:
    '下一步:': 'Next steps:',

    # 下拉框取值异常，请重新选择。
    '下拉框取值异常，请重新选择。': 'Unexpected dropdown value, please pick again.',

    # 不一致
    '不一致': 'differs',

    # 不一致 ✘
    '不一致 ✘': 'does not match ✘',

    # 仅预览（不写文件）
    '仅预览（不写文件）': 'Preview only (write nothing)',

    # 任意字节，可重复。OFF 支持别名或十六进制（如 0x1B1
    '任意字节，可重复。OFF 支持别名或十六进制（如 0x1B1=1）': (
        'Any byte, repeatable. OFF accepts an alias or hex (e.g. 0x1B1=1)'
    ),

    # 低区 [11]
    '低区 [11]': 'low region [11]',

    # 修改字段并写出新档案
    '修改字段并写出新档案': 'Modify fields and write a new profile',

    # 值 %d 超出 %d 字节范围
    '值 %d 超出 %d 字节范围': 'Value %d does not fit in %d byte(s)',

    # 值不合法
    '值不合法': 'Invalid value',

    # 值可以是十进制或 0x 开头的十六进制。
    '值可以是十进制或 0x 开头的十六进制。': 'The value may be decimal, or hex with a 0x prefix.',

    # 值越界
    '值越界': 'Value out of range',

    # 偏移 0x%X (size %d) 超出 Setup 变量范
    '偏移 0x%X (size %d) 超出 Setup 变量范围 [0, 0x%X)': (
        'Offset 0x%X (size %d) is outside the Setup variable range [0, 0x%X)'
    ),

    # 偏移 0x%X 变成 %02X，预期 %s
    '偏移 0x%X 变成 %02X，预期 %s': 'Offset 0x%X became %02X, expected %s',

    # 偏移 0x%X 超出 Setup 变量范围 [0, 0x%X
    '偏移 0x%X 超出 Setup 变量范围 [0, 0x%X)': (
        'Offset 0x%X is outside the Setup variable range [0, 0x%X)'
    ),

    # 偏移越界
    '偏移越界': 'Offset out of range',

    # 写出失败
    '写出失败': 'Write failed',

    # 切换后界面立刻重建，已载入的档案、待改动与日志都会保留
    '切换后界面立刻重建，已载入的档案、待改动与日志都会保留': (
        'Switching rebuilds the UI immediately; the loaded profile, pending changes and log'
        ' are all kept'
    ),

    # 列出已收录的板型
    '列出已收录的板型': 'List known boards',

    # 删除选中
    '删除选中': 'Remove selected',

    # 加入改动：Setup+0x%03X %s = %s
    '加入改动：Setup+0x%03X %s = %s': 'Added change: Setup+0x%03X %s = %s',

    # 区域
    '区域': 'region',

    # 原值
    '原值': 'Old',

    # 原始数据长度 %d 与目标 Setup 块大小 %d 不一致
    '原始数据长度 %d 与目标 Setup 块大小 %d 不一致': (
        'Raw data length %d does not match the target Setup block size %d'
    ),

    # 变体数
    '变体数': 'variants',

    # 另存为…
    '另存为…': 'Save as…',

    # 只看改动，不写文件
    '只看改动，不写文件': 'Only show the changes, write nothing',

    # 否（⇒ 新状态）
    '否（⇒ 新状态）': 'no (⇒ new state)',

    # 启动时直接打开的档案（可选）
    '启动时直接打开的档案（可选）': 'Profile to open on startup (optional)',

    # 大小:
    '大小:': 'Size:',

    # 大小: %d 字节
    '大小: %d 字节': 'Size: %d bytes',

    # 字段
    '字段': 'Field',

    # 字段偏移表只对已实测的板型负责。请先用 `info` 核对几
    '字段偏移表只对已实测的板型负责。请先用 `info` 核对几条已知量是否合理：\n    Setup+0x1A8 DRAM Voltage 应是合理内存电压（如 1200–1500 mV），\n    且 Setup+0x1A6 VTT_DDR 约为它的一半。\n    数值明显不合理 ⇒ **不要改**，先把 `info` 输出贴到 issue 补字段表。': (
        'The field offset table is only guaranteed for boards that were actually tested. First'
        ' check a few known values with `info`:'
        '\n    Setup+0x1A8 DRAM Voltage should be a sane memory voltage (say 1200–1500 mV),'
        '\n    and Setup+0x1A6 VTT_DDR should be about half of it.'
        '\n    Obviously wrong numbers ⇒ **do not write** — open an issue and paste your'
        ' `info` output so the table can be extended.'
    ),

    # 字段别名（如 cpu-llc）或十六进制偏移（如 0x190
    '字段别名（如 cpu-llc）或十六进制偏移（如 0x190）': 'Field alias (e.g. cpu-llc) or hex offset (e.g. 0x190)',

    # 字段自检异常：Setup+0x1A8 DRAM Voltag
    '字段自检异常：Setup+0x1A8 DRAM Voltage = %d mV，但 Setup+0x1A6 VTT_DDR = %d mV；DDR4 下 VTT_DDR 应约为 DRAM 的一半。\n    这说明字段偏移表与本档案不匹配（板型/BIOS 版本不同），**请不要继续改写**。': (
        'Field self-check failed: Setup+0x1A8 DRAM Voltage = %d mV but Setup+0x1A6 VTT_DDR ='
        ' %d mV; on DDR4, VTT_DDR should be about half of DRAM.'
        '\n    This means the field offset table does not match this profile (different'
        ' board/BIOS version) — **please stop and do not write**.'
    ),

    # 字节值必须在 0..255。
    '字节值必须在 0..255。': 'Byte value must be 0..255.',

    # 字节值必须在 0..255（Setup+0x%03X 给的是
    '字节值必须在 0..255（Setup+0x%03X 给的是 %d）': (
        'Byte value must be 0..255 (Setup+0x%03X was given %d)'
    ),

    # 字节级 diff (%d 处):
    '字节级 diff (%d 处):': 'Byte-level diff (%d change(s)):',

    # 完成
    '完成': 'Done',

    # 导出 Setup 原始数据
    '导出 Setup 原始数据': 'Export the raw Setup data',

    # 将把 %d 字节写入 Setup 块，产生 %d 处改动
    '将把 %d 字节写入 Setup 块，产生 %d 处改动': (
        'Will write %d bytes into the Setup block, producing %d change(s)'
    ),

    # 将改动的文件字节 (%d 处，仅预览):
    '将改动的文件字节 (%d 处，仅预览):': 'File bytes that would change (%d change(s), preview only):',

    # 将要改动:
    '将要改动:': 'About to change:',

    # 尾是否已在别处出现
    '尾是否已在别处出现': 'tail seen elsewhere?',

    # 已写出:
    '已写出:': 'Written:',

    # 已写出：
    '已写出：\n%s\n\n请把它拷到 FAT32 U 盘根目录，再进 BIOS 用 “Load User Default from USB flash drive” 载入。': (
        'Written:'
        '\n%s'
        '\n'
        '\nCopy it to the root of a FAT32 USB stick, then load it in the BIOS via “Load User'
        ' Default from USB flash drive”.'
    ),

    # 已把 %s 记为基线（下次检测会做基线对照，判定 100%%
    '已把 %s 记为基线（下次检测会做基线对照，判定 100%% 确定）': (
        '%s recorded as the baseline (the next detection compares against it and is 100%%'
        ' certain)'
    ),

    # 已收录的板型:
    '已收录的板型:': 'Known boards:',

    # 已清空待改动列表
    '已清空待改动列表': 'Pending changes cleared',

    # 已知字段当前值:
    '已知字段当前值:': 'Known field values:',

    # 应用并写出
    '应用并写出': 'Apply & write',

    # 当前 CPU Load-Line Calibration =
    '当前 CPU Load-Line Calibration = %s': 'Current CPU Load-Line Calibration = %s',

    # 当前值
    '当前值': 'Value',

    # 所有文件
    '所有文件': 'All files',

    # 手工指定 Setup 变量在文件里的偏移（默认自动探测）
    '手工指定 Setup 变量在文件里的偏移（默认自动探测）': (
        'Set the Setup variable offset manually (auto-detected by default)'
    ),

    # 手工指定 Setup 变量大小（默认自动探测）
    '手工指定 Setup 变量大小（默认自动探测）': (
        'Set the Setup variable size manually (auto-detected by default)'
    ),

    # 打开图形界面（可跟一个档案路径）
    '打开图形界面（可跟一个档案路径）': 'Open the GUI (optionally followed by a profile path)',

    # 打开失败
    '打开失败': 'Open failed',

    # 打开档案…
    '打开档案…': 'Open profile…',

    # 打开输出目录
    '打开输出目录': 'Open output folder',

    # 找不到文件
    '找不到文件': 'File not found',

    # 找不到文件 %s
    '找不到文件 %s': 'File not found: %s',

    # 把 Setup 原始数据塞回档案
    '把 Setup 原始数据塞回档案': 'Inject raw Setup data back into a profile',

    # 指定的值与当前值相同，无需写出。
    '指定的值与当前值相同，无需写出。': 'The specified value equals the current one; nothing to write.',

    # 探测: %s
    '探测: %s': 'Detection: %s',

    # 整片 SPI 镜像（AFU 备份的 .bin）或 U 盘配置
    '整片 SPI 镜像（AFU 备份的 .bin）或 U 盘配置档案': (
        'Full SPI image (a .bin backed up by AFU) or a USB config profile'
    ),

    # 文件: %s
    '文件: %s': 'File: %s',

    # 文件太小（%d 字节），不像 ASRock 配置档案
    '文件太小（%d 字节），不像 ASRock 配置档案': 'File is too small (%d bytes) to be an ASRock profile',

    # 文件头不是可打印 ASCII，可能不是 ASRock 配置档
    '文件头不是可打印 ASCII，可能不是 ASRock 配置档案（前 32 字节: %r）': (
        'The file header is not printable ASCII — this may not be an ASRock profile (first 32'
        ' bytes: %r)'
    ),

    # 文件长度变了（%d → %d）
    '文件长度变了（%d → %d）': 'File length changed (%d → %d)',

    # 新值
    '新值': 'New',

    # 无法构造改动
    '无法构造改动': 'Cannot build the change',

    # 无法自动定位 Setup 变量块。请用 --setup-of
    '无法自动定位 Setup 变量块。请用 --setup-offset 手工指定\n  （--setup-offset 要给「长度前缀 + 4」之后的偏移，即 Setup 变量数据的起始位置）': (
        'Cannot locate the Setup variable block automatically. Pass --setup-offset manually'
        '\n  (--setup-offset takes the offset *after* the 4-byte length prefix, i.e. where the'
        ' Setup data starts)'
    ),

    # 无法解析档案
    '无法解析档案': 'Cannot parse profile',

    # 无法识别
    '无法识别': 'Not recognised',

    # 无法识别的字段 %r。可用别名: %s；也可以直接给十六进制
    '无法识别的字段 %r。可用别名: %s；也可以直接给十六进制偏移（如 0x190）': (
        'Unrecognised field %r. Aliases: %s; you can also pass a hex offset (e.g. 0x190)'
    ),

    # 是（⇒ 冻结链）
    '是（⇒ 冻结链）': 'yes (⇒ frozen chain)',

    # 显示档案信息 + 已知字段当前值
    '显示档案信息 + 已知字段当前值': 'Show profile info + current values of known fields',

    # 未收录也不一定不能用 —— 只要档案结构相同、字段值看着合理
    '未收录也不一定不能用 —— 只要档案结构相同、字段值看着合理即可。': (
        'Not being listed does not mean it will not work — as long as the profile layout'
        ' matches and the field values look sane.'
    ),

    # 未选择镜像
    '未选择镜像': 'No image selected',

    # 板型/版本不匹配时工具会打 warning；`info` 也
    '板型/版本不匹配时工具会打 warning；`info` 也会做一次数值自检': (
        'On a board/version mismatch the tool prints a warning; `info` also runs a numeric'
        ' self-check'
    ),

    # 板型: %r   ← %s
    '板型: %r   ← %s': 'Board: %r   ← %s',

    # 板型: %r   ← ⚠ 未收录的板型
    '板型: %r   ← ⚠ 未收录的板型': 'Board: %r   ← ⚠ board not in the known list',

    # 板型不匹配：档案板型是 %r，本工具只收录了 %s。
    '板型不匹配：档案板型是 %r，本工具只收录了 %s。\n    %s': (
        'Board mismatch: the profile says %r, but this tool only knows %s.'
        '\n    %s'
    ),

    # 档案体长度: 0x%X (%d)
    '档案体长度: 0x%X (%d)': 'Profile body length: 0x%X (%d)',

    # 档案里的 CPU LLC = %s
    '档案里的 CPU LLC = %s': 'CPU LLC in the profile = %s',

    # 档案里读不到板型字段（头 32 字节为空）—— 可能不是 A
    '档案里读不到板型字段（头 32 字节为空）—— 可能不是 ASRock 配置档案。': (
        'No board field in the profile (the first 32 bytes are empty) — this may not be an'
        ' ASRock profile.'
    ),

    # 检测
    '检测': 'Detect',

    # 检测失败
    '检测失败': 'Detection failed',

    # 检测失败：未找到 Setup 副本
    '检测失败：未找到 Setup 副本': 'Detection failed: no Setup copy found',

    # 检测失败：链尾记录不完整
    '检测失败：链尾记录不完整': 'Detection failed: the tail record is incomplete',

    # 检测当前 CPU LLC 是几级（读整片 SPI 镜像）
    '检测当前 CPU LLC 是几级（读整片 SPI 镜像）': 'Detect the current CPU LLC level (reads a full SPI image)',

    # 没有实际改动。
    '没有实际改动。': 'No actual change.',

    # 没有指定任何要修改的字段。
    '没有指定任何要修改的字段。': 'No field was specified for modification.',

    # 没有指定要改什么。用 --llc / --soc-llc /
    '没有指定要改什么。用 --llc / --soc-llc / --byte，或 -h 看帮助': (
        'Nothing to change. Use --llc / --soc-llc / --byte, or -h for help'
    ),

    # 没有改动
    '没有改动': 'No changes',

    # 注意
    '注意': 'Note',

    # 添加
    '添加': 'Add',

    # 清空改动
    '清空改动': 'Clear changes',

    # 用作基线
    '用作基线': 'Use as baseline',

    # 用户取消了操作（版型警告）。
    '用户取消了操作（版型警告）。': 'User cancelled (board warning).',

    # 界面/输出语言（默认自动：$ASR_LANG → 终端编码 
    '界面/输出语言（默认自动：$ASR_LANG → 终端编码 → 系统语言）。写在子命令前后都可以': (
        'UI/output language (default: auto — $ASR_LANG → terminal encoding → system locale).'
        ' May be placed before or after the subcommand'
    ),

    # 界面已切换到新写出档案：%s
    '界面已切换到新写出档案：%s': 'The UI switched to the newly written profile: %s',

    # 界面语言
    '界面语言': 'UI language',

    # 直接覆盖原文件
    '直接覆盖原文件': 'Overwrite the source file',

    # 直接覆盖原文件（强烈建议先备份）
    '直接覆盖原文件（强烈建议先备份）': 'Overwrite the source file (a backup is strongly recommended)',

    # 示例:
    '示例:\n  asrock_profile                              # 打开图形界面\n  asrock_profile info pbo2-test\n  asrock_profile set  pbo2-test --llc 3\n  asrock_profile set  pbo2-test --soc-llc 5 -o soc5\n  asrock_profile set  pbo2-test --byte 0x1B1=1 --dry-run\n  asrock_profile detect r5.bin --baseline r4.bin\n': (
        'Examples:'
        '\n  asrock_profile                              # open the GUI'
        '\n  asrock_profile info pbo2-test'
        '\n  asrock_profile set  pbo2-test --llc 3'
        '\n  asrock_profile set  pbo2-test --soc-llc 5 -o soc5'
        '\n  asrock_profile set  pbo2-test --byte 0x1B1=1 --dry-run'
        '\n  asrock_profile detect r5.bin --baseline r4.bin'
        '\n'
    ),

    # 符合 DDR4 的 2:1 关系 ✔
    '符合 DDR4 的 2:1 关系 ✔': 'matches the DDR4 2:1 ratio ✔',

    # 类型: U 盘配置档案（板型 %r / BIOS %r）
    '类型: U 盘配置档案（板型 %r / BIOS %r）': 'Type: USB config profile (board %r / BIOS %r)',

    # 类型: 整片 SPI 镜像
    '类型: 整片 SPI 镜像': 'Type: full SPI image',

    # 自动探测命中 %d 个候选；选用偏移最小的：长度前缀 @0x
    '自动探测命中 %d 个候选；选用偏移最小的：长度前缀 @0x%X、size=0x%X、小字节比例 %.0f%%': (
        'Auto-detection found %d candidate(s); using the lowest-addressed one: length prefix'
        ' @0x%X, size=0x%X, small-byte ratio %.0f%%'
    ),

    # 自定义字节（偏移支持别名或十六进制，如 0x1B1）
    '自定义字节（偏移支持别名或十六进制，如 0x1B1）': 'Custom byte (an alias or a hex offset, e.g. 0x1B1)',

    # 若 info 显示的 Setup 变量位置不是 0x59、或
    '若 info 显示的 Setup 变量位置不是 0x59、或自动探测失败，': (
        'If `info` reports a Setup variable offset other than 0x59, or if auto-detection'
        ' fails,'
    ),

    # 要写入的 Setup 原始数据（大小必须完全一致）
    '要写入的 Setup 原始数据（大小必须完全一致）': 'Raw Setup data to write (the size must match exactly)',

    # 覆盖原文件（建议先备份）
    '覆盖原文件（建议先备份）': 'Overwrite the source file (back it up first)',

    # 解析失败
    '解析失败': 'Parse failed',

    # 请先打开一个配置档案。
    '请先打开一个配置档案。': 'Open a profile first.',

    # 请先选择一个 SPI 镜像。
    '请先选择一个 SPI 镜像。': 'Please choose an SPI image first.',

    # 请先选择一张整片 SPI 镜像（用 AFU 的 /O 备份出
    '请先选择一张整片 SPI 镜像（用 AFU 的 /O 备份出来的 .bin）。\n也可以直接选一份 U 盘配置档案，但那样只能看到档案里的值。': (
        "Choose a full SPI image first (a .bin backed up with AFU's /O)."
        '\nYou may also pick a USB config profile, but then you only see the value stored in'
        ' the profile.'
    ),

    # 请同时填写偏移和值。
    '请同时填写偏移和值。': 'Fill in both the offset and the value.',

    # 请改用命令行模式，例如: %s info <档案>
    '请改用命令行模式，例如: %s info <档案>\n': (
        'Use the command line instead, e.g. %s info <profile>'
        '\n'
    ),

    # 请用 --setup-offset / --setup-si
    '请用 --setup-offset / --setup-size 手工指定。': (
        'specify it by hand with --setup-offset / --setup-size.'
    ),

    # 读取单个字段
    '读取单个字段': 'Read a single field',

    # 读取失败
    '读取失败': 'Read failed',

    # 载入: %s（%d 字节，Setup @0x%X size=
    '载入: %s（%d 字节，Setup @0x%X size=0x%X）': 'Loaded: %s (%d bytes, Setup @0x%X size=0x%X)',

    # 输入不完整
    '输入不完整': 'Incomplete input',

    # 输出文件
    '输出文件': 'Output file',

    # 输出文件（默认 <输入>.mod）
    '输出文件（默认 <输入>.mod）': 'Output file (default <input>.mod)',

    # 输出档案另存为
    '输出档案另存为': 'Save the output profile as',

    # 输出路径与输入相同。要覆盖原文件请显式加 --inplace
    '输出路径与输入相同。要覆盖原文件请显式加 --inplace（建议先备份）': (
        'Output path equals the input path. To overwrite in place, pass --inplace explicitly'
        ' (back up first)'
    ),

    # 还没有载入档案
    '还没有载入档案': 'No profile loaded',

    # 选择 ASRock 配置档案
    '选择 ASRock 配置档案': 'Choose an ASRock profile',

    # 选择整片 SPI 镜像（AFU 的 /O 备份产物）
    '选择整片 SPI 镜像（AFU 的 /O 备份产物）': 'Choose a full SPI image (an AFU /O backup)',

    # 选择镜像…
    '选择镜像…': 'Choose image…',

    # 重新载入
    '重新载入': 'Reload',

    # 重新载入失败
    '重新载入失败': 'Reload failed',

    # 链头(最旧)
    '链头(最旧)': 'head(oldest)',

    # 链尾(最新)
    '链尾(最新)': 'tail(newest)',

    # 链长
    '链长': 'chain',

    # 错误:
    '错误: ': 'Error: ',

    # 错误: 无法加载 GUI（本机 Python 没带 tkin
    '错误: 无法加载 GUI（本机 Python 没带 tkinter）。\n': (
        'Error: cannot load the GUI (this Python has no tkinter).'
        '\n'
    ),

    # 长度前缀位置: 0x%X (%d)
    '长度前缀位置: 0x%X (%d)': 'Length-prefix offset: 0x%X (%d)',

    # 顺便跟一份配置档案比对（核对载入的值是否真的生效）
    '顺便跟一份配置档案比对（核对载入的值是否真的生效）': (
        'Also compare against a config profile (to check whether the loaded value really took'
        ' effect)'
    ),

    # 顺带把 Setup 原始数据导出到 OUT
    '顺带把 Setup 原始数据导出到 OUT': 'Also export the raw Setup data to OUT',

    # 高区 [10]
    '高区 [10]': 'high region [10]',

    # （可用 --setup-offset 0x%X --setu
    '（可用 --setup-offset 0x%X --setup-size 0x%X 把它塞回另一个档案）': (
        '(use --setup-offset 0x%X --setup-size 0x%X to inject it into another profile)'
    ),

    # （未检测）
    '（未检测）': '(not detected)',

    # （用 DRAM Voltage 与 VTT_DDR 的 2:
    '（用 DRAM Voltage 与 VTT_DDR 的 2:1 关系判断偏移表是否还对得上）。': (
        '(using the DRAM Voltage vs VTT_DDR 2:1 ratio to tell whether the offset table still'
        ' lines up).'
    ),

    # ：尺寸不是常见的 0x280，请用 info 核对字段值是否
    '：尺寸不是常见的 0x280，请用 info 核对字段值是否合理': (
        ': size is not the usual 0x280 — check the field values with `info`'
    ),
}



if __name__ == "__main__":
    sys.exit(main())
