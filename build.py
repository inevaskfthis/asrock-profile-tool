#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键打包单文件可执行文件（GUI + CLI 同一个文件）。

    python build.py

产物：`dist/asrock_profile.exe`（Windows）/ `dist/asrock_profile`（Linux、macOS）

为什么用 --console 而不是 --noconsole：
一个可执行文件只能绑定一个子系统。CLI 模式必须有 stdout，而 GUI 模式多一个控制台窗口
只是不好看、不影响功能（那个窗口正好当详细日志看）。反过来用 --noconsole，
命令行模式会完全没有输出 —— 所以选 --console。

⚠ 重新打包前请先退出正在运行的程序：Windows 上文件被占用时删不掉旧的 dist 产物，
  PyInstaller 会失败，而失败信息很容易被 `| tail -3` 吃掉，结果你以为打包成功、
  实际 dist/ 里还是旧版本。本脚本会在删不掉时**明确报错并停下**，打包后再验一次版本。
"""
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "asrock_profile"
IS_WIN = sys.platform == "win32"


def fail(msg):
    print("\n[打包失败] " + msg)
    return 1


def rm_rf(path):
    """删目录；Windows 上文件被占用时给出可操作的提示而不是堆栈。"""
    if not os.path.isdir(path):
        return True
    try:
        shutil.rmtree(path)
        return True
    except OSError as e:
        print("[x] 无法删除 %s：%s" % (path, e))
        if IS_WIN:
            print("    → 最常见的原因：程序还在运行，文件被占用。")
            print("      taskkill /F /IM %s.exe      （或者直接关掉那个窗口）" % NAME)
        else:
            print("    → 检查是否有进程还在用这个目录：lsof +D %s" % path)
        return False


def main():
    try:
        import PyInstaller                                    # noqa: F401
    except ImportError:
        print("缺少 PyInstaller，先执行:  %s -m pip install pyinstaller" % sys.executable)
        return 1

    for d in ("build", "dist"):
        if not rm_rf(os.path.join(HERE, d)):
            return 1

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--console",
        "--name", NAME,
        "--distpath", "dist",
        "--workpath", "build",
        "--specpath", "build",
        "asrock_profile.py",
    ]
    print("执行:", " ".join(cmd))
    r = subprocess.run(cmd, cwd=HERE)
    if r.returncode != 0:
        return fail("PyInstaller 返回 %d" % r.returncode)

    exe = os.path.join(HERE, "dist", NAME + ".exe")
    path = exe if os.path.exists(exe) else os.path.join(HERE, "dist", NAME)
    if not os.path.exists(path):
        return fail("没找到产物 %s" % path)

    # ---- 打包后自检：跑一遍 --version，确认装的确实是刚编译出来的那份 ----
    look = re.search(r'__version__\s*=\s*"([^"]+)"',
                     open(os.path.join(HERE, "asrock_profile.py"), encoding="utf-8").read())
    want = look.group(1) if look else None
    try:
        got = subprocess.run([path, "--version"], capture_output=True, timeout=120)
        text = (got.stdout or b"").decode("utf-8", errors="replace").strip()
    except Exception as e:                                     # noqa: BLE001
        return fail("产物跑不起来：%r" % e)

    print("\n完成: %s  (%.1f MB)" % (path, os.path.getsize(path) / 1048576.0))
    print("  内置版本: %s" % text)
    if want and want not in text:
        return fail("版本对不上：源码是 %s，产物报 %r —— dist 里很可能是**旧的**产物"
                    "（打包前记得先退出正在运行的程序）" % (want, text))
    print("  ✔ 版本自检通过")
    print()
    print("  双击 / 不带参数  → GUI")
    print("  带子命令         → CLI，例如  %s info <档案>" % os.path.basename(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
