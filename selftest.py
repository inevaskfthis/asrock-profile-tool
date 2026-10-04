# -*- coding: utf-8 -*-
"""asrock_profile.py 冒烟测试

覆盖：
  1. CLI 帮助 / 版本 / 板型表
  2. info / get
  3. set 正常路径 + 与手工产物逐字节比对
  4. dry-run 不落文件
  5. 错误处理（都应 exit 1，且不抛 traceback）
  6. dump → inject 往返（恒等）
  7. **版型 / BIOS 版本不匹配 → warning**（新）
  8. **字段自检（DRAM/VTT 2:1）→ warning**（新）
  9. **GUI 构建与载入冒烟**（新，需要图形环境）

用真实档案：默认 H:/pbo2-test。可用环境变量 ASR_SRC 指定别的。
"""
import hashlib
import os
import re
import struct
import subprocess
import sys
import tempfile

PY = sys.executable
IS_WIN = sys.platform == "win32"
HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, 'asrock_profile.py')
SRC = os.environ.get("ASR_SRC", r"H:/pbo2-test")
TMP = tempfile.mkdtemp(prefix="asr_test_")

#: 工具现在会自动探测语言（POSIX 上 LANG=en_US.UTF-8 会选英文），
#: 不把语言钉住的话，下面所有中文断言在 Linux / CI 上都会挂。
ENV_ZH = dict(os.environ, ASR_LANG="zh", PYTHONIOENCODING="utf-8")
ENV_EN = dict(os.environ, ASR_LANG="en", PYTHONIOENCODING="utf-8")

results = []
SYNTHETIC = False
HAVE_SRC = True


def run(*args, expect=0):
    r = subprocess.run([PY, TOOL] + list(args), capture_output=True, text=True,
                       encoding='utf-8', errors='replace', env=ENV_ZH)
    ok = (r.returncode == expect)
    print("  [%s] %s" % ("PASS" if ok else "FAIL", " ".join(str(a) for a in args[:4])))
    if not ok:
        print("      returncode=%s (期望 %s)" % (r.returncode, expect))
        print("      stdout:", (r.stdout or "")[:500])
        print("      stderr:", (r.stderr or "")[:500])
    return r, ok


class _FakeBox(object):
    """替掉 tkinter 的 messagebox，避免测试被弹窗卡住。"""
    def __init__(self):
        self.calls = []
    def showinfo(self, *a, **k): self.calls.append(("info", a))
    def showwarning(self, *a, **k): self.calls.append(("warn", a))
    def showerror(self, *a, **k): self.calls.append(("error", a))
    def askyesno(self, *a, **k): self.calls.append(("ask", a)); return True


def make_synthetic_profile(path):
    """按文档里的格式造一份**格式合法**的配置档案。

    用途：本机没有真实档案时（Linux / macOS / CI 上不会有 `H:` 盘或 r4/r5 镜像）
    也能跑通结构性用例。它只保证「格式对」，不代表任何真机的实际取值。

    结构见 README「档案格式」一节：
        0x00 板型 32B | 0x20 版本 32B | 0x38 02 00 | 0x3A u32 档案体长度
        0x55 u32 Setup 变量长度前缀 | 0x59 起 640 字节 Setup 变量原样副本 | 之后全零
    """
    import struct as _struct
    buf = bytearray(61525)
    buf[0:32] = b"A1818" + b"\x00" * 27
    buf[0x20:0x40] = b" 10.50" + b"\x00" * 26
    buf[0x38] = 0x02
    _struct.pack_into("<I", buf, 0x3A, 0xD62)
    _struct.pack_into("<I", buf, 0x55, 0x280)
    off = 0x59
    setup = buf[off:off + 0x280]
    setup[0x190] = 0                      # CPU LLC  = Auto
    setup[0x191] = 3                      # SOC LLC  = Level 3
    _struct.pack_into("<H", setup, 0x1A6, 650)     # VTT_DDR
    _struct.pack_into("<H", setup, 0x1A8, 1300)    # DRAM Voltage
    setup[0x262] = 1
    buf[off:off + 0x280] = setup
    with open(path, "wb") as f:
        f.write(bytes(buf))
    return path


def check(label, cond):
    print("      %s %s" % ("✔" if cond else "✘", label))
    results.append(bool(cond))
    return cond


if not os.path.exists(SRC):
    # 没有真实档案（典型：Linux / macOS / CI）⇒ 合成一份格式合法的，让结构性用例照跑
    SRC = make_synthetic_profile(os.path.join(TMP, "synthetic-profile"))
    SYNTHETIC = True

print("=" * 70)
print("0. 环境")
print("=" * 70)
if SYNTHETIC:
    print("  源档案: **合成本地档案**（本机没有真实档案）")
    print("          %s" % SRC)
    print("  说明: set ASR_SRC=<真实档案> 可换成真档案；依赖真档案的用例会自动跳过。")
else:
    print("  源档案: %s" % SRC)
print("  平台: %s (%s)" % (sys.platform, "Windows" if IS_WIN else "POSIX"))

print("=" * 70)
print("1. 帮助 / 版本 / 板型表")
print("=" * 70)
results.append(run("-h")[1])
results.append(run("--version")[1])
r, ok = run("boards"); results.append(ok)
check("boards 列出 A1818", "A1818" in r.stdout)

print("=" * 70)
print("2. info / get")
print("=" * 70)
r, ok = run("info", SRC); results.append(ok)
check("Setup 变量位置 = 0x59", "Setup 变量位置: 0x59" in r.stdout)
check("SOC LLC = Level 3（与实测一致）",
      "0x191  VDDCR_SOC Load-Line Calibration                = 3 (Level 3)" in r.stdout)
check("板型识别为 A1818", "'A1818'" in r.stdout)
check("干净档案无版型警告", "版型警告" not in r.stdout)

r, ok = run("get", SRC, "cpu-llc"); results.append(ok)
check("cpu-llc = Auto", "0 (Auto)" in r.stdout)

print("=" * 70)
print("3. set：正常路径 + 与手工产物逐字节比对")
print("=" * 70)
out = os.path.join(TMP, "out_llc3")
results.append(run("set", SRC, "--llc", "3", "-o", out)[1])
manual = r"H:/cpullc-L3"
if os.path.exists(manual) and not SYNTHETIC:
    a = open(out, 'rb').read(); b = open(manual, 'rb').read()
    check("与手工产物 H:/cpullc-L3 逐字节相同", a == b)
else:
    print("      （跳过：源档案是合成的，跟真机导出的产物没有可比性）")

print("=" * 70)
print("4. set：dry-run 不应产生文件")
print("=" * 70)
out2 = os.path.join(TMP, "should_not_exist")
results.append(run("set", SRC, "--llc", "5", "-o", out2, "--dry-run")[1])
check("dry-run 未写文件", not os.path.exists(out2))

print("=" * 70)
print("5. 错误处理（都应 exit 1，且不抛 traceback）")
print("=" * 70)
r, ok = run("set", SRC, "--llc", "9", expect=1); results.append(ok)
check("非法 LLC 值被拒", "0..5" in (r.stderr or ""))
r, ok = run("set", SRC, "--byte", "0xFFF=1", expect=1); results.append(ok)
check("越界偏移被拒", "超出" in (r.stderr or ""))
r, ok = run("set", SRC, expect=1); results.append(ok)
check("空操作被拒", "没有指定" in (r.stderr or ""))
r, ok = run("info", os.path.join(TMP, "nonexistent"), expect=1); results.append(ok)
check("文件不存在", "找不到文件" in (r.stderr or ""))
r, ok = run("get", SRC, "no-such-field", expect=1); results.append(ok)
check("未知字段别名", "无法识别" in (r.stderr or ""))

r = subprocess.run([PY, TOOL, "info", SRC], capture_output=True, text=True,
                   encoding='utf-8', errors='replace')
check("无 Python traceback 泄漏", "Traceback" not in (r.stdout + r.stderr))

print("=" * 70)
print("6. dump → inject 往返（应为恒等）")
print("=" * 70)
raw = os.path.join(TMP, "setup.bin")
results.append(run("dump", SRC, raw)[1])
check("导出 640 字节", os.path.getsize(raw) == 0x280)
back = os.path.join(TMP, "roundtrip")
r, ok = run("inject", SRC, raw, "-o", back); results.append(ok)
if os.path.exists(back):
    check("往返后与原文件逐字节相同", open(SRC, 'rb').read() == open(back, 'rb').read())
else:
    check("inject 无改动时不写文件（恒等，符合预期）", "没有任何改动" in r.stdout)

print("=" * 70)
print("7. 版型不匹配 → warning")
print("=" * 70)
src_bytes = bytearray(open(SRC, 'rb').read())


def make_variant(name, mutate):
    b = bytearray(src_bytes)
    mutate(b)
    p = os.path.join(TMP, name)
    open(p, 'wb').write(bytes(b))
    return p


# 7a. 板型改成不认识的
bad_board = make_variant("bad_board", lambda b: b.__setitem__(
    slice(0, 32), b"A9999" + b"\x00" * 27))
r, ok = run("info", bad_board); results.append(ok)
check("info 打出「版型警告」", "版型警告" in r.stdout)
check("提示板型未收录", "A9999" in r.stdout)

# 7b. 板型对但 BIOS 版本不认识
bad_ver = make_variant("bad_ver", lambda b: b.__setitem__(
    slice(0x20, 0x40), b" 9.99" + b"\x00" * 28))
r, ok = run("info", bad_ver); results.append(ok)
check("版本未收录也警告", "版型警告" in r.stdout and "9.99" in r.stdout)

# 7c. set 时也要带 warning
out3 = os.path.join(TMP, "bad_out")
r, ok = run("set", bad_board, "--llc", "3", "-o", out3); results.append(ok)
check("set 时同样打出 warning", "版型警告" in r.stdout)

print("=" * 70)
print("8. 字段自检（DRAM vs VTT_DDR 2:1 关系）")
print("=" * 70)
# Setup 起始 0x59，+0x1A8 = DRAM Voltage 改成 800 mV（VTT 仍是 650 ⇒ 2*650=1300 ≠ 800）
bad_dram = make_variant("bad_dram", lambda b: struct.pack_into("<H", b, 0x59 + 0x1A8, 800))
r, ok = run("info", bad_dram); results.append(ok)
check("数值不自洽时报警", "字段自检异常" in r.stdout)
check("提示不要继续改", "不要继续改写" in r.stdout)

print("=" * 70)
print("9. GUI 构建冒烟（需要图形环境）")
print("=" * 70)
try:
    import tkinter as tk
    sys.path.insert(0, HERE)
    import asrock_profile as M

    check("gui_available() == True", M.gui_available())

    root = tk.Tk()
    root.withdraw()                                    # 不弹窗
    app = M.create_gui(root, SRC)
    root.update()

    check("GUI 载入档案成功", app.p is not None)
    check("字段表已填充 (%d 行) " % len(app.tree_fields.get_children()),
          len(app.tree_fields.get_children()) >= 15)
    check("信息面板非空", len(app.txt_info.get("1.0", "end").strip()) > 40)
    check("干净档案显示 ✔ 通过", "通过" in app.lbl_warn.cget("text"))

    # 切换到「不匹配板型」的档案，警告标签应变红字
    app.load_file(bad_board)
    root.update()
    check("不匹配板型时警告标签有内容", len(app.lbl_warn.cget("text")) > 20)
    check("警告文本含板型号", "A9999" in app.lbl_warn.cget("text"))

    # 加载回好档案，测「添加待改动 + 走内部写出通道」
    app.load_file(SRC)
    root.update()
    app.v_off.set("0x1B1")
    app.v_val.set("1")
    app.on_add_pending()
    root.update()
    check("待改动列表已加入 1 条", len(app.pending) == 1)

    # on_apply 会弹 messagebox，用顶层的假对象替代
    app.messagebox = _FakeBox()
    app.v_out.set(os.path.join(TMP, "gui_out"))
    app.v_dryrun.set(True)
    app.on_apply()
    root.update()
    logtxt = app.txt_log.get("1.0", "end")
    check("GUI 预览路径产出「将改动的文件字节」", "将改动的文件字节" in logtxt)
    check("GUI 预览路径未写文件", not os.path.exists(os.path.join(TMP, "gui_out")))

    app.v_dryrun.set(False)
    app.v_out.set(os.path.join(TMP, "gui_out"))
    app.load_file(SRC)
    root.update()
    app.v_off.set("0x1B1")
    app.v_val.set("1")
    app.on_add_pending()
    app.on_apply()
    root.update()
    check("GUI 写出成功", os.path.exists(os.path.join(TMP, "gui_out")))
    if os.path.exists(os.path.join(TMP, "gui_out")):
        a = open(SRC, 'rb').read(); b = open(os.path.join(TMP, "gui_out"), 'rb').read()
        check("GUI 写出只改了 1 字节且长度一致",
              len(a) == len(b) and sum(1 for i in range(len(a)) if a[i] != b[i]) == 1)

    root.destroy()
except ImportError:
    print("      （跳过：本机无 tkinter）")
except Exception as e:
    import traceback
    traceback.print_exc()
    check("GUI 冒烟未抛异常: %r" % e, False)

print("=" * 70)
print("10. 源文件未被修改（所有操作都是只读的）")
print("=" * 70)
h1 = hashlib.sha256(open(SRC, 'rb').read()).hexdigest()[:32]
print("      pbo2-test sha256 = %s" % h1)
check("源文件 sha256 保持不变", h1 == hashlib.sha256(bytes(src_bytes)).hexdigest()[:32])

print("=" * 70)
print("11. GBK 控制台编码回归（Windows cmd 默认 cp936，曾把 exe 打挂）")
print("=" * 70)
# ⚠/✔/✘/⇒/↔ 不在 GBK 里；PyInstaller 的 exe 在 cmd 里跑 warning 分支曾直接 traceback。
# 用 PYTHONIOENCODING=gbk 复现该环境。
env_gbk = dict(os.environ, PYTHONIOENCODING="gbk", ASR_LANG="zh")
r = subprocess.run([PY, TOOL, "info", bad_board], capture_output=True,
                   env=env_gbk)
so = (r.stdout or b"").decode("gbk", errors="replace")
se = (r.stderr or b"").decode("gbk", errors="replace")
check("GBK 下 info 不崩（returncode=0）", r.returncode == 0)
check("GBK 下无 traceback", "Traceback" not in (so + se))
check("GBK 下 warning 用 ASCII 替身 [!]", "[!]" in so and "\u26a0" not in so)
check("GBK 下中文正常（不是乱码）", "板型不匹配" in so)

r = subprocess.run([PY, TOOL, "set", bad_board, "--llc", "3",
                    "-o", os.path.join(TMP, "gbk_out")],
                   capture_output=True, env=env_gbk)
so = (r.stdout or b"").decode("gbk", errors="replace")
se = (r.stderr or b"").decode("gbk", errors="replace")
check("GBK 下 set（warning + 自检）不崩", r.returncode == 0)
check("GBK 下自检标记为 [OK]", "[OK] 自检通过" in so)

print("=" * 70)
print("12. detect：检测当前 CPU LLC（需要整片 SPI 镜像）")
print("=" * 70)
R4 = os.environ.get("ASR_R4", r"C:/bios_work/r4.bin")
R5 = os.environ.get("ASR_R5", r"C:/bios_work/r5.bin")

if os.path.exists(R4) and os.path.exists(R5):
    # r4 是用户"还没改 CPU LLC"时读的；r5 是"载入 cpullc-L3 之后"读的
    r, ok = run("detect", R4); results.append(ok)
    check("r4 判定 = Auto（与当时实际一致）",
          "当前 CPU Load-Line Calibration = 0 (Auto)" in r.stdout)
    check("r4 找到链表", "链尾(最新)" in r.stdout)

    r, ok = run("detect", R5); results.append(ok)
    check("r5 判定 = Level 3（与用户实测一致）",
          "当前 CPU Load-Line Calibration = 3 (Level 3)" in r.stdout)
    check("r5 交叉核对 DRAM/VTT 2:1 通过", "符合 DDR4 的 2:1 关系" in r.stdout)

    r, ok = run("detect", R5, "--baseline", R4); results.append(ok)
    check("基线模式给出确定性依据", "**基线对照**" in r.stdout)
    check("基线模式结论仍为 Level 3",
          "当前 CPU Load-Line Calibration = 3 (Level 3)" in r.stdout)
    check("列出新增副本的取值分布",
          "新增副本的 +0x190 分布" in r.stdout and "Level 3" in r.stdout)

    r, ok = run("detect", R5, "--expect", SRC); results.append(ok)
    check("--expect 比对生效（pbo2-test 里是 Auto ⇒ 不一致）",
          "不一致" in r.stdout)
else:
    print("  （跳过：未找到 r4.bin / r5.bin，可用环境变量 ASR_R4 / ASR_R5 指定）")

r, ok = run("detect", SRC); results.append(ok)
check("给小档案时提示「这是档案里的值，不是当前生效值」",
      "不是主板上当前生效的值" in r.stdout)
check("小档案路径给出档案里的 CPU LLC",
      "档案里的 CPU LLC = 0 (Auto)" in r.stdout)

r, ok = run("detect", SRC, "--expect", SRC); results.append(ok)
check("--expect 自己跟自己 ⇒ 一致", "一致" in r.stdout and "不一致" not in r.stdout)

print("=" * 70)
print("13. detect 的 GUI 路径")
print("=" * 70)
if os.path.exists(R5):
    try:
        import tkinter as tk
        sys.path.insert(0, HERE)
        import asrock_profile as M

        root = tk.Tk(); root.withdraw()
        app = M.create_gui(root, SRC)
        app.messagebox = _FakeBox()
        app.v_spi.set(R5)
        app.on_detect()
        root.update()
        check("GUI 检测出 Level 3",
              "Level 3" in app.lbl_detect.cget("text"))
        check("GUI 日志含链表与结论",
              "链尾(最新)" in app.txt_log.get("1.0", "end"))
        root.destroy()
    except ImportError:
        print("      （跳过：本机无 tkinter）")
else:
    print("  （跳过：未找到 r5.bin）")

print("=" * 70)
print("14. 跨平台检查")
print("=" * 70)
sys.path.insert(0, HERE)
import asrock_profile as M                                        # noqa: E402

check("plat_key() 归一化到三档之一", M.plat_key() in ("win32", "darwin", "linux"))
for k in ("win32", "darwin", "linux"):
    check("字体表覆盖 %s（界面 + 等宽）" % k,
          bool(M.UI_FONT_PREFS.get(k)) and bool(M.MONO_FONT_PREFS.get(k)))
check("pick_font 命中第一个可用",
      M.pick_font(["DejaVu Sans", "Noto Sans CJK SC", "X"],
                  M.UI_FONT_PREFS["linux"], None) == "Noto Sans CJK SC")
check("pick_font 大小写不敏感",
      M.pick_font(["consolas"], M.MONO_FONT_PREFS["win32"], None) == "Consolas")
check("pick_font 全落空时回退", M.pick_font([], M.UI_FONT_PREFS["linux"], "FB") == "FB")

# 目录当输入 / 当输出 —— POSIX 与 Windows 都会抛 OSError，两边都必须是干净报错
r, ok = run("info", TMP, expect=1); results.append(ok)
check("输入是目录 ⇒ 干净报错", "Traceback" not in (r.stdout or "") + (r.stderr or ""))
r, ok = run("set", SRC, "--llc", "3", "-o", TMP, expect=1); results.append(ok)
check("输出目标是目录 ⇒ 干净报错", "Traceback" not in (r.stdout or "") + (r.stderr or ""))

# 终端编码扛不住中文时的两种表现（v1.4.0 起：**自动切英文**，不再只给一条提示）
# ① 没指定语言 ⇒ 自动选英文（提示本身就不需要了）
env_ascii = dict(os.environ, PYTHONIOENCODING="ascii")
env_ascii.pop("ASR_LANG", None)
r = subprocess.run([PY, TOOL, "info", bad_board], capture_output=True, env=env_ascii)
raw = (r.stdout or b"") + (r.stderr or b"")
txt = raw.decode("ascii", "replace")
check("ascii 终端 ⇒ 自动切英文", "Board mismatch" in txt)
check("ascii 终端 ⇒ 输出确实全是 ASCII", all(b < 0x80 for b in raw))
check("ascii 终端 ⇒ 无 traceback", "Traceback" not in txt)
check("ascii 终端 ⇒ 仍正常退出", r.returncode == 0)

# ② 明确要求中文 ⇒ 保留中文 + 打那条纯 ASCII 的英文提示
env_ascii_zh = dict(os.environ, PYTHONIOENCODING="ascii", ASR_LANG="zh")
r = subprocess.run([PY, TOOL, "--version"], capture_output=True, env=env_ascii_zh)
txt = ((r.stdout or b"") + (r.stderr or b"")).decode("ascii", "replace")
check("ascii + 强制 zh ⇒ 打出英文提示",
      "cannot render this tool's Chinese output" in txt)
check("ascii + 强制 zh ⇒ 不崩", r.returncode == 0 and "Traceback" not in txt)

env_utf8 = dict(os.environ, PYTHONIOENCODING="utf-8", ASR_LANG="zh")
r = subprocess.run([PY, TOOL, "--version"], capture_output=True, env=env_utf8)
txt = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", "replace")
check("utf-8 终端 ⇒ 不打扰用户", "cannot render" not in txt)

src_text = open(TOOL, encoding="utf-8").read()
check("没留下裸 os.system（跨平台 shell 差异）", "os.system(" not in src_text)
check("os.startfile 有平台守卫", "os.startfile" in src_text and IS_WIN is not None)

print("=" * 70)
print("15. i18n：中英双语")
print("=" * 70)
CJK_RE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")

# ---- 15.1 英文模式下所有子命令都不能漏出中文 ----
EN_CASES = [
    ["-h"], ["info", "-h"], ["set", "-h"], ["detect", "-h"], ["boards", "-h"],
    ["boards"], ["info", SRC], ["get", SRC, "cpu-llc"],
    ["set", SRC, "--llc", "5", "--dry-run"],
    ["set", SRC, "--llc", "9"],                 # 错误路径
    ["info", TMP],                              # OSError 路径
    ["detect", SRC],
]
leaked = []
for c in EN_CASES:
    r = subprocess.run([PY, TOOL] + c, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=ENV_EN)
    out = (r.stdout or "") + (r.stderr or "")
    hits = sorted(set(CJK_RE.findall(out)))
    if hits:
        leaked.append((c, "".join(hits)[:20]))
check("英文模式：%d 个命令零漏译" % len(EN_CASES), not leaked)
if leaked:
    for c, h in leaked[:6]:
        print("        漏译 %s -> %s" % (" ".join(c), h))

# ---- 15.2 --lang 可以写在子命令**后面**（main 里会先摘掉它） ----
r, ok = run("boards", "--lang", "zh"); results.append(ok)
check("--lang 写在子命令后面也被接受", "A1818" in (r.stdout or ""))

r = subprocess.run([PY, TOOL, "--lang", "xx", "boards"], capture_output=True,
                   text=True, encoding="utf-8", errors="replace", env=ENV_ZH)
check("拼错的 --lang 给出警告而不是静默吞掉",
      "unknown --lang" in (r.stderr or ""))

# ---- 15.3 译文表自身的完整性（用 ast —— 它会自动合并隐式拼接的字面量） ----
import ast                                                       # noqa: E402
tree = ast.parse(src_text)
wrapped_keys = set()
for node in ast.walk(tree):
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "T"):
        for a in node.args:
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                wrapped_keys.add(a.value)

print("      源码里被 T() 包住的字面量 %d 条；EN 表 %d 条"
      % (len(wrapped_keys), len(M.EN)))
check("没有未翻译的字面量", not (wrapped_keys - set(M.EN)))
check("EN 表里没有多余 / 失效的 key", not (set(M.EN) - wrapped_keys))
check("EN 表全部非空", all(v.strip() for v in M.EN.values()))
check("EN 表的值里没有中文（否则等于没译）",
      not any(CJK_RE.search(v) for v in M.EN.values()))

# 占位符必须逐一对齐，否则 % 格式化会崩或错位
ph = re.compile(r"%(.)")
bad_ph = [k for k, v in M.EN.items()
          if sorted(ph.findall(k)) != sorted(ph.findall(v))]
check("占位符（%s/%d/%X/%%…）全部对齐", not bad_ph)
if bad_ph:
    for k in bad_ph[:5]:
        print("        %r -> %r" % (k[:60], M.EN[k][:60]))

# ---- 15.4 语言探测 ----
check("normalize_lang 认得常见写法",
      M.normalize_lang("zh-CN") == "zh" and M.normalize_lang("en_US") == "en"
      and M.normalize_lang("Chinese") == "zh" and M.normalize_lang("nope") is None)
check("lang_name 各用各的写法", M.lang_name("zh") == "中文" and M.lang_name("en") == "English")
M.set_lang("en"); check("set_lang('en') 生效", M.T("文件: %s") == "File: %s")
M.set_lang("zh"); check("set_lang('zh') 生效", M.T("文件: %s") == "文件: %s")
M.set_lang("nope"); check("认不出的语言退回自动判断", M.get_lang() in ("zh", "en"))

# ---- 15.5 GUI 切语言 ----
if os.environ.get("ASR_NO_GUI") != "1":
    try:
        import tkinter as tk
        M.set_lang("zh")
        root = tk.Tk(); root.withdraw()
        app = M.create_gui(root, SRC)
        app.messagebox = _FakeBox()
        root.update()
        check("GUI 初始为中文", CJK_RE.search(root.title()) is not None)
        app.v_lang.set("English"); app.on_lang_change(); root.update()
        check("GUI 切英文后标题无中文", CJK_RE.search(root.title()) is None)
        check("GUI 切英文后日志无中文",
              CJK_RE.search(app.txt_log.get("1.0", "end")) is None)
        check("GUI 切英文后哨兵文本是英文", app.v_llc.get() == "(no change)")
        check("GUI 切英文后档位下拉内容也是英文",
              all(CJK_RE.search(v) is None for v in app._llc_values()))
        check("GUI 切英文后 _llc_num 仍认得旧值", app._llc_num("(no change)") is None
              and app._llc_num("Level 3 (3)") == 3)
        app.v_lang.set("中文"); app.on_lang_change(); root.update()
        check("GUI 切回中文", CJK_RE.search(root.title()) is not None)
        root.destroy()
        M.set_lang("zh")
    except ImportError:
        print("      （跳过：本机无 tkinter）")
else:
    print("      （跳过：ASR_NO_GUI=1）")

print()
print("=" * 70)
print("结果: %d / %d 通过" % (sum(1 for x in results if x), len(results)))
print("=" * 70)

sys.exit(0 if all(results) else 1)
