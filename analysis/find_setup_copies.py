# -*- coding: utf-8 -*-
"""用 Setup 变量 payload 的特征串，在 r4/r5 全片定位所有副本（不限 NVAR 大小字段）。"""
import re, struct

r4 = open(r"C:/bios_work/r4.bin", 'rb').read()
r5 = open(r"C:/bios_work/r5.bin", 'rb').read()

# 从已知副本取特征：Setup payload 的前 16 字节
SIG = bytes.fromhex("010000010101 00000101000101000101".replace(' ',''))
print("特征串 (%d B): %s" % (len(SIG), SIG.hex(' ')))

def find(d, tag):
    hits = []
    for m in re.finditer(re.escape(SIG), d):
        p = m.start()
        # 看前 10 字节上下文，判断是不是 NVAR 里的 payload
        ctx = d[max(0, p - 24):p]
        hits.append((p, ctx))
    print("\n[%s] 命中 %d 处" % (tag, len(hits)))
    for p, ctx in hits:
        # 该位置往后 0x280 看电压字节
        v190 = d[p + 0x190] if p + 0x191 < len(d) else None
        v191 = d[p + 0x191] if p + 0x191 < len(d) else None
        nm = re.search(rb'([\x20-\x7E]{4,20})\x00$', ctx)
        print("   @0x%06X  +0x190=%02X +0x191=%02X   前文名=%s" % (
            p, v190, v191, (nm.group(1).decode() if nm else '?')))
    return [h[0] for h in hits]

A = find(r4, 'r4')
B = find(r5, 'r5')
print("\n=== 位置对比 ===")
print("  仅 r4 有: %s" % ", ".join("0x%X" % x for x in set(A) - set(B)) or "  (无)")
print("  仅 r5 有: %s" % ", ".join("0x%X" % x for x in set(B) - set(A)) or "  (无)")
print("  两边都有: %s" % ", ".join("0x%X" % x for x in sorted(set(A) & set(B))))

print("\n=== 直接找 r5 里值为 03 且前后像 Setup 的位置 ===")
# 在 r5 里找所有 [0x190]==3 的 Setup 副本
for p in B:
    print("  @0x%06X  [0x190]=%02X [0x191]=%02X [0x1A6]=%02X [0x1A8]=%02X" % (
        p, r5[p + 0x190], r5[p + 0x191], r5[p + 0x1A6], r5[p + 0x1A8]))
