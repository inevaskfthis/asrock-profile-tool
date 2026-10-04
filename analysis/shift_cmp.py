# -*- coding: utf-8 -*-
"""直接验：固定位移 d 下 A[i] vs B[i+d] 的最长连续相同段。
比锚点法可靠 —— 没有哈希、没有合并启发式，纯逐字节。"""
KBODY = r"C:/bios_work/verify_stock/stock.bin.dump/42 4F1C52D3-D824-4D2A-A2F0-EC40C23C5916/7 9E21FD93-9C72-4C15-8C4B-E77F1DB2D792/0 EE4E5898-3914-4259-9D6E-DC7BD79403CF/1 Volume image section/0 5C60F367-A505-419A-859E-2A4FF6CA6FE5/145 Setup/1 PE32 image section/body.bin"
K4BODY = r"C:/bios_work/verify_k4/k4.bin.dump/41 4F1C52D3-D824-4D2A-A2F0-EC40C23C5916/7 9E21FD93-9C72-4C15-8C4B-E77F1DB2D792/0 EE4E5898-3914-4259-9D6E-DC7BD79403CF/1 Volume image section/0 5C60F367-A505-419A-859E-2A4FF6CA6FE5/145 Setup/1 PE32 image section/body.bin"

A = open(KBODY, 'rb').read()    # Killer
B = open(K4BODY, 'rb').read()   # K4
print("Killer %d B  K4 %d B" % (len(A), len(B)))

def runs_at_shift(d, minlen=4096):
    """A[i] == B[i+d] 的所有 >= minlen 的连续段"""
    lo = max(0, -d); hi = min(len(A), len(B) - d)
    if hi <= lo:
        return []
    out = []
    start = None
    for i in range(lo, hi):
        if A[i] == B[i + d]:
            if start is None:
                start = i
        else:
            if start is not None and i - start >= minlen:
                out.append((start, i, start + d, i + d))
            start = None
    if start is not None and hi - start >= minlen:
        out.append((start, hi, start + d, hi + d))
    return out

print("\n=== 固定位移 d=-189 (Killer 比 K4 长 160 B 之外的净增) ===")
for r in runs_at_shift(-189):
    print("  Killer[0x%06X:0x%06X] len=%-7d <-> K4[0x%06X:0x%06X]" % (
        r[0], r[1], r[1] - r[0], r[2], r[3]))

print("\n=== 固定位移 d=-384 ===")
for r in runs_at_shift(-384):
    print("  Killer[0x%06X:0x%06X] len=%-7d <-> K4[0x%06X:0x%06X]" % (
        r[0], r[1], r[1] - r[0], r[2], r[3]))

print("\n=== 固定位移 d=0（同偏移直接比）===")
for r in runs_at_shift(0):
    print("  Killer[0x%06X:0x%06X] len=%-7d <-> K4 同偏移" % (r[0], r[1], r[1] - r[0]))

# 全片：统计"同偏移下相同"的字节数
same0 = sum(1 for i in range(min(len(A), len(B))) if A[i] == B[i])
print("\n同偏移相同字节: %d / %d = %.1f%%" % (same0, min(len(A), len(B)),
                                        100.0 * same0 / min(len(A), len(B))))
