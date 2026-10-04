# -*- coding: utf-8 -*-
"""收官复核：Killer 10.50 vs K4 10.50 四个决定性事实，一次算完。"""
import os, re, hashlib, struct

STOCK = r"C:/bios_work/verify_stock/stock.bin.dump"
K4    = r"C:/bios_work/verify_k4/k4.bin.dump"
R1    = r"H:/bios_work/r1.bin"

def sha(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()

def find_mod(root, name):
    """返回模块目录（形如 '23 IR35201Pei'）"""
    for dp, dn, fn in os.walk(root):
        for d in dn:
            if re.match(r'^\d+\s+' + re.escape(name) + r'$', d):
                return os.path.join(dp, d)
    return None

def body(moddir, sub='1 TE image section'):
    for cand in (os.path.join(moddir, sub, 'body.bin'),
                 os.path.join(moddir, '1 PE32 image section', 'body.bin')):
        if os.path.exists(cand):
            return cand
    # 兜底：找任意 image section
    for dp, dn, fn in os.walk(moddir):
        if 'body.bin' in fn and 'image section' in dp:
            return os.path.join(dp, 'body.bin')
    return None

print("=" * 78)
print("Killer 10.50  vs  K4 10.50   —   收官复核")
print("=" * 78)

# ---------- 1. 模块清单 ----------
print("\n[1] 模块级 GUID 集合")
for tag, root in (("Killer", STOCK), ("K4", K4)):
    csv = root.replace('.dump', '.guids.csv')
    lines = [l.strip() for l in open(csv, encoding='utf-8', errors='replace') if l.strip()]
    print("    %-7s %d 行" % (tag, len(lines)))
a = set(open(STOCK.replace('.dump', '.guids.csv'), encoding='utf-8', errors='replace').read().split('\n'))
b = set(open(K4.replace('.dump', '.guids.csv'), encoding='utf-8', errors='replace').read().split('\n'))
print("    仅 Killer 有 : %d" % len(a - b))
print("    仅 K4 有     : %d" % len(b - a))

# ---------- 2. Setup 出厂默认变量 ----------
print("\n[2] Setup 出厂默认变量  NVAR store/0 StdDefaults/0 Setup/body.bin")
ps = os.path.join(STOCK, "1 FA4974FC-AF1D-4E5D-BDC5-DACD6D27BAEC/0 NVAR store/0 StdDefaults/0 Setup/body.bin")
pk = None
for dp, dn, fn in os.walk(K4):
    if 'StdDefaults' in dp and re.search(r'[/\\]Setup$', dp) and 'body.bin' in fn:
        pk = os.path.join(dp, 'body.bin')
        break
if not pk:
    for dp, dn, fn in os.walk(K4):
        if 'StdDefaults' in dp:
            for d in dn:
                if d.endswith('Setup'):
                    p = os.path.join(dp, d, 'body.bin')
                    if os.path.exists(p):
                        pk = p
                        break
        if pk:
            break
print("    Killer : %s" % ps)
print("    K4     : %s" % pk)
ds, dk = open(ps, 'rb').read(), open(pk, 'rb').read()
print("    size   : %d  vs  %d   %s" % (len(ds), len(dk), "SAME" if len(ds) == len(dk) else "DIFF!"))
print("    sha256 : %s" % sha(ps)[:32])
print("    sha256 : %s" % sha(pk)[:32])
diffs = [i for i in range(min(len(ds), len(dk))) if ds[i] != dk[i]]
print("    字节差异: %d 处  ->  %s" % (
    len(diffs), ", ".join("0x%03X: %02X->%02X" % (i, ds[i], dk[i]) for i in diffs)))

# ---------- 3. IR35201Pei 驱动 ----------
print("\n[3] IR35201Pei（Infineon IR35201 多相 VRM 控制器驱动）")
for tag, root in (("Killer", STOCK), ("K4", K4)):
    m = find_mod(root, "IR35201Pei")
    bp = body(m)
    d = open(bp, 'rb').read()
    print("    %-7s %s" % (tag, bp))
    print("            %d B   sha256 %s" % (len(d), sha(bp)[:32]))
bs = open(body(find_mod(STOCK, "IR35201Pei")), 'rb').read()
bk = open(body(find_mod(K4, "IR35201Pei")), 'rb').read()
print("    size   : %d  vs  %d   %s" % (len(bs), len(bk), "SAME" if len(bs) == len(bk) else "DIFF!"))
dd = [i for i in range(min(len(bs), len(bk))) if bs[i] != bk[i]]
print("    字节差异: %d 处" % len(dd))
for i in dd:
    dlt = bk[i] - bs[i]
    print("      0x%04X : %02X -> %02X   (delta %+d)%s" % (
        i, bs[i], bk[i], dlt, "   <-- +/-1 = TE 重定位" if abs(dlt) == 1 else "   <-- !!! 非重定位"))

# ---------- 4. 活体 Setup 记录（r1.bin 基线） ----------
print("\n[4] 活体 Setup 记录（r1.bin，写探针之前）")
r1 = open(R1, 'rb').read()
OFFS = [(0x18B, 'CPU Vcore Voltage mode'), (0x18C, 'CPU Vcore Fixed V'),
        (0x190, 'CPU Load-Line Calibration'), (0x191, 'VDDCR_SOC LL Calibration'),
        (0x194, 'CPU Vcore Offset V'), (0x196, 'VDDCR_SOC Voltage mode'),
        (0x197, 'VDDCR_SOC Fixed V'), (0x19B, 'VDDCR_SOC Offset V'),
        (0x1A4, 'VDDP'), (0x1A6, 'VTT_DDR'), (0x1AA, '1.05V_PROM Voltage'),
        (0x1AC, 'CPU VDD 1.8 Voltage'), (0x1AE, '2.50V_PROM Voltage'),
        (0x1B0, 'VPPM'), (0x1B1, 'VDDCR SOC Voltage'), (0x1B2, 'Vcore Offset Voltage')]
base = 0x620B7
print("    记录 NVAR @ 0x620A6  payload base 0x%05X" % base)
for off, nm in OFFS:
    p = base + off
    print("      +0x%03X  0x%06X  %-26s = %02X" % (off, p, nm, r1[p]))

# ---------- 5. 16 MB 全片里 k4 是否也含这些偏移的 Setup 记录 ----------
print("\n[5] 结论摘要")
print("    Setup 默认变量字节差异           : %d" % len(diffs))
print("    IR35201Pei 字节差异              : %d (全部 ±1 ? %s)" % (
    len(dd), all(abs(bk[i] - bs[i]) == 1 for i in dd)))
