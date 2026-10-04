"""交互模式测试驱动（由 selftest 第 16 节调用）。

用假的 tty 包住 asrock_profile 的 stdin/stdout，让交互模式以为有人在操作。
路径从环境变量 IA_HOME 取，避免把绝对路径写死在这个文件里。
"""
import io
import os
import sys

sys.path.insert(0, os.environ["IA_HOME"])
import asrock_profile as M

ARGV = list(sys.argv[1:])
sys.argv = ["asrock_profile"] + ARGV
SCRIPT = os.environ["IA_SCRIPT"].split("|")


class FakeTTY(io.StringIO):
    """看起来像 tty 的 stdin：isatty() 为真，按脚本逐行吐，脚本用尽则返回 EOF。"""

    def __init__(self):
        self.lines = list(SCRIPT)
        super().__init__()

    def isatty(self):
        return True

    def fileno(self):
        raise OSError("fake tty has no fileno")

    def readline(self, *a):
        return (self.lines.pop(0) + "\n") if self.lines else ""

    def read(self, *a):
        return self.readline()


fake = FakeTTY()
M._interactive = lambda: True
M.sys.stdin = fake
M.sys.stdout = fake
try:
    rc = M.main(ARGV)
finally:
    M.sys.stdout = sys.__stdout__
    sys.__stdout__.write(fake.getvalue())
    sys.__stdout__.write("\n[exit=%s]\n" % rc)
