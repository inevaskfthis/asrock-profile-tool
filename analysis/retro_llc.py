# -*- coding: utf-8 -*-
"""复盘用：逐条复核 Killer 与 K4 的 CPU Load-Line Calibration 到底是什么关系。"""

import hashlib
import os
import re

V = r"C:/Users/Administrator/WorkBuddy/2026-08-31-07-51-46/v580"
KS = r"C:/bios_work/verify_stock/stock.bin.dump"
K4 = r"C:/bios_work/verify_k4/k4.bin.dump"

K_MOD = (KS + "/42 4F1C52D3-D824-4D2A-A2F0-EC40C23C5916/7 9E21FD93-9C72-4C15-8C4B-E77F1DB2D792/"
         "0 EE4E5898-3914-4259-9D6E-DC7BD79403CF/1 Volume image section/"
         "0 5C60F367-A505-419A-859E-2A4FF6CA6FE5/145 Setup/1 PE32 image section/body.bin")
K4_MOD = (K4 + "/41 4F1C52D3-D824-4D2A-A2F0-EC40C23C5916/7 9E21FD93-9C72-4C15-8C4B-E77F1DB2D792/"
          "0 EE4E5898-3914-4259-9D6E-DC7BD79403CF/1 Volume image section/"
          "0 5C60F367-A505-419A-859E-2A4FF6CA6FE5/145 Setup/1 PE32 image section/body.bin")
K_STD = KS + "/1 FA4974FC-AF1D-4E5D-BDC5-DACD6D27BAEC/0 NVAR store/0 StdDefaults/0 Setup/body.bin"
K4_STD = K4 + "/1 FA4974FC-AF1D-4E5D-BDC5-DACD6D27BAEC/0 NVAR store/0 StdDefaults/0 Setup/body.bin"
IFR_K = V + "/kil10.50_setup_orig.bin.0.0.en-US.uefi.ifr.txt"
IFR_K4 = V + "/k410.50_setup.bin.0.0.en-US.uefi.ifr.txt"
G_K = KS + "/stock.bin.guids.csv"
G_K4 = K4 + "/k4.bin.guids.csv"


def sha(p, n=16):
    return hashlib.sha256(open(p, 'rb').read()).hexdigest()[:n]


print("=" * 84)
print("① IFR 里 VarOffset 0x190 的引用次数（决定「菜单里有没有这一项」）")
print("=" * 84)
for tag, p in (("Killer", IFR_K), ("K4", IFR_K4)):
    t = open(p, encoding='utf-8', errors='replace').read()
    refs = re.findall(r"VarOffset:\s*0x190,", t)
    print("  %-7s VarOffset 0x190 引用 %d 次" % (tag, len(refs)))
    for m in re.finditer(r"^.*VarOffset:\s*0x190,.*$", t, re.M):
        print("         %s" % m.group(0).strip()[:150])

print()
print("=" * 84)
print("② 同一个偏移在两版 IFR 里各自对应什么条目（QId 体系完全不同）")
print("=" * 84)
for off in ("0x190", "0x191", "0x1B1", "0x1B2"):
    print("  --- VarOffset %s ---" % off)
    for tag, p in (("Killer", IFR_K), ("K4", IFR_K4)):
        t = open(p, encoding='utf-8', errors='replace').read().split('\n')
        found = []
        for i, l in enumerate(t):
            if "VarOffset: %s," % off in l:
                qm = re.search(r"QuestionId:\s*(0x[0-9A-Fa-f]+)", l)
                # 往上找 Prompt
                nm = "?"
                for j in range(i, max(0, i - 12), -1):
                    pm = re.search(r'Prompt:\s*"(.*?)"', t[j])
                    if pm:
                        nm = pm.group(1)
                        break
                found.append("%s (QId %s)" % (nm, qm.group(1) if qm else "?"))
        print("      %-7s %s" % (tag, " ｜ ".join(found) if found else "** 零引用 **"))
    print()

print("=" * 84)
print("③ 孤儿字符串：CPU Load-Line Calibration 在 Setup 模块里还在不在")
print("=" * 84)
for tag, p in (("Killer", K_MOD), ("K4", K4_MOD)):
    d = open(p, 'rb').read()
    name = b"CPU Load-Line Calibration"
    # IFR 里的字符串是 UTF-16LE
    u16 = name.decode().encode("utf-16-le")
    print("  %-7s body %d B  ASCII 命中 %d 处 | UTF-16LE 命中 %d 处 %s"
          % (tag, len(d), d.count(name), d.count(u16),
             ["0x%X" % m.start() for m in re.finditer(re.escape(u16), d)][:6]))

print()
print("=" * 84)
print("④ Setup 模块 body 级对比（菜单模块本身）")
print("=" * 84)
a = open(K_MOD, 'rb').read()
b = open(K4_MOD, 'rb').read()
print("  Killer %d B / K4 %d B  (差 %+d)" % (len(a), len(b), len(a) - len(b)))
print("  sha256  Killer %s / K4 %s" % (sha(K_MOD), sha(K4_MOD)))

# 固定位移下逐字节比对，找最长相同段
best = None
for d in range(-512, 513):
    i = max(0, -d)
    j = max(0, d)
    n = min(len(a) - i, len(b) - j)
    if n <= 0:
        continue
    cnt = sum(1 for k in range(n) if a[i + k] == b[j + k])
    if best is None or cnt > best[0]:
        best = (cnt, d)
print("  最佳固定位移 d=%d 时，%d / %d 字节相同（%.1f%%）"
      % (best[1], best[0], min(len(a), len(b)), 100.0 * best[0] / min(len(a), len(b))))

# 该位移下的连续相同段
d = best[1]
i = max(0, -d)
j = max(0, d)
runs = []
cur = None
for k in range(min(len(a) - i, len(b) - j)):
    if a[i + k] == b[j + k]:
        if cur is None:
            cur = [i + k, j + k, 1]
        else:
            cur[2] += 1
    else:
        if cur and cur[2] >= 4096:
            runs.append(tuple(cur))
        cur = None
if cur and cur[2] >= 4096:
    runs.append(tuple(cur))
print("  位移 %d 下的连续相同段（≥4096 B）：" % d)
for ka, kb, n in runs:
    print("     Killer 0x%06X..0x%06X (%d B)  ≡  K4 0x%06X..0x%06X"
          % (ka, ka + n, n, kb, kb + n))
print("  ⇒ 相同段合计 %d B，占模块 %.1f%%" % (sum(r[2] for r in runs),
                                             100.0 * sum(r[2] for r in runs) / len(a)))
diff_lo = min([r[0] for r in runs] + [len(a)]) if runs else 0
print("  ⇒ 差异集中在 Killer 的 0x%06X 之前（约 %d KB）"
      % (diff_lo, diff_lo // 1024))

print()
print("=" * 84)
print("⑤ 出厂默认 Setup 变量（决定「字段布局」—— 这一版是两板共享的定义）")
print("=" * 84)
sa = open(K_STD, 'rb').read()
sb = open(K4_STD, 'rb').read()
print("  Killer %d B / K4 %d B" % (len(sa), len(sb)))
dd = [k for k in range(min(len(sa), len(sb))) if sa[k] != sb[k]]
print("  逐字节差异 %d 处：%s" % (len(dd), " ".join("+0x%X:%02X/%02X" % (k, sa[k], sb[k]) for k in dd)))
for off in (0x190, 0x191, 0x1A6, 0x1A8):
    print("     +0x%03X  Killer=%02X  K4=%02X" % (off, sa[off], sb[off]))

print()
print("=" * 84)
print("⑥ 模块清单 / 关键驱动对比")
print("=" * 84)
ga = {l.split(',')[0].strip() for l in open(G_K, encoding='utf-8', errors='replace') if l.strip()}
gb = {l.split(',')[0].strip() for l in open(G_K4, encoding='utf-8', errors='replace') if l.strip()}
print("  GUID 数：Killer %d / K4 %d；差集 Killer-K4 %d 个、K4-Killer %d 个"
      % (len(ga), len(gb), len(ga - gb), len(gb - ga)))
