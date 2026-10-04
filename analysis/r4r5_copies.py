# -*- coding: utf-8 -*-
"""对每个 Setup 特征串位置，列出 r4 / r5 的 +0x190 / +0x191，并标出变化。"""
import re

r4 = open(r"C:/bios_work/r4.bin", 'rb').read()
r5 = open(r"C:/bios_work/r5.bin", 'rb').read()
r1 = open(r"C:/bios_work/r1.bin", 'rb').read() if __import__('os').path.exists(r"C:/bios_work/r1.bin") else None

SIG = bytes.fromhex("01000001010100000101000101000101")

def hits(d):
    return [m.start() for m in re.finditer(re.escape(SIG), d)]

A, B = hits(r4), hits(r5)
print("r4 命中 %d 处 / r5 命中 %d 处" % (len(A), len(B)))
allp = sorted(set(A) | set(B))

print("\n%-10s %-8s %-8s %-8s %-8s %s" % ("位置", "r4_190", "r5_190", "r4_191", "r5_191", "备注"))
print("-" * 66)
changed = []
for p in allp:
    a190 = r4[p + 0x190] if p in A and p + 0x191 < len(r4) else None
    a191 = r4[p + 0x191] if p in A and p + 0x191 < len(r4) else None
    b190 = r5[p + 0x190] if p in B and p + 0x191 < len(r5) else None
    b191 = r5[p + 0x191] if p in B and p + 0x191 < len(r5) else None
    note = ""
    if a190 != b190 or a191 != b191:
        note = "  <<< 变化"
        changed.append(p)
    if p not in A:
        note += "  <<< r5 新增"
    if p not in B:
        note += "  <<< r5 没了"
    f = lambda v: "--" if v is None else "%02X" % v
    # 只打印有意义的：190 或 191 非零，或位置有变化
    interesting = (a190 or a191 or b190 or b191 or note)
    if interesting:
        print("0x%06X  %-8s %-8s %-8s %-8s%s" % (p, f(a190), f(b190), f(a191), f(b191), note))

print("\n=== 汇总 ===")
print("  变化的副本位置: %s" % (", ".join("0x%X" % p for p in changed) or "(无)"))
print("\n=== r5 里 +0x190 == 03 的副本 ===")
for p in B:
    if p + 0x191 < len(r5) and r5[p + 0x190] == 3:
        print("  0x%06X  +0x190=03 +0x191=%02X  %s" % (
            p, r5[p + 0x191], "r4 同位置也是 03" if p in A and r4[p + 0x190] == 3 else "** r4 时不是 03 **"))
