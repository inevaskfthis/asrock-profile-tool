# -*- coding: utf-8 -*-
"""验证 0x04B695 等处是否是真的完整 Setup 副本（而非低熵特征串的巧合命中）。
   判据：整段 0x280 与已知真副本的相关性 + 结构字段是否合理。"""
import re

r5 = open(r"C:/bios_work/r5.bin", 'rb').read()
# 已知真副本：@0x041FF4（权威）与 @0x0620B7（旧快照）
REF = r5[0x0620B7:0x0620B7 + 0x280]

CAND = [0x04B695, 0x04BD4C, 0x04BFD6, 0x04C2A5, 0x04C52F, 0x04DD0F, 0x04E7CC, 0x04EA56,
        0x048479, 0x04972D, 0x04AC12, 0x04C7CF, 0x04D28C]

print("参考副本 @0x0620B7 (已知真副本) 的字段：")
for o in (0x190, 0x191, 0x1A6, 0x1A8, 0x1AA, 0x1AC, 0x262, 0x233, 0x27A):
    print("   +0x%03X = %02X" % (o, REF[o]))

print("\n各候选位置的完整比对（与参考副本逐字节差异数）：")
print("%-10s %-8s %-8s %-8s %-8s %-8s %-8s" % ("位置", "差异数", "190", "191", "1A6", "1A8", "262"))
for p in CAND:
    blk = r5[p:p + 0x280]
    if len(blk) < 0x280:
        continue
    dd = sum(1 for i in range(0x280) if blk[i] != REF[i])
    print("0x%06X  %-8d %-8s %-8s %-8s %-8s %-8s" % (
        p, dd, "%02X" % blk[0x190], "%02X" % blk[0x191],
        "%02X" % blk[0x1A6], "%02X" % blk[0x1A8], "%02X" % blk[0x262]))

print("\n=== 关键判定 ===")
print("若某候选与参考副本差异数很小（<80）且字段结构合理 ⇒ 是真副本；")
print("若差异数 >200 ⇒ 只是低熵特征串的巧合命中，不可信。")

print("\n=== 候选区 0x04B680 起的十六进制（看整体是否像 Setup）===")
for i in range(0x04B680, 0x04B6D0, 16):
    c = r5[i:i + 16]
    print("  %06X  %-47s  |%s|" % (i, c.hex(' '),
          ''.join(chr(x) if 32 <= x < 127 else '.' for x in c)))
