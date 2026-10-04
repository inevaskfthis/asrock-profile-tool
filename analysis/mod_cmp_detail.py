# -*- coding: utf-8 -*-
"""10.50 两版电压相关模块：逐模块字节差异 + Setup 偏移消费方扫描。"""
import os, re, hashlib, struct

ROOTS = {"Killer": r"C:/bios_work/verify_stock/stock.bin.dump",
         "K4":     r"C:/bios_work/verify_k4/k4.bin.dump"}
SETUP_GUID = bytes.fromhex("43D687ECA4EBB54BA1E53F3E36B20DA9")
KEYS = ('Aod', 'IR35201', 'NCT3933', 'CpuPei', 'NbPei', 'GnB', 'CbsSetup',
        'AmdPbsSetup', 'PbsSetup', 'Nvram', 'Setup', 'TcgPlatform')

def collect(root):
    out = {}
    for dp, dn, fn in os.walk(root):
        if 'body.bin' not in fn:
            continue
        if not re.search(r'[/\\]\d+\s+(PE32|TE)\s+image section$', dp):
            continue
        base = os.path.basename(os.path.dirname(dp))
        m = re.match(r'^\d+\s+(.+)$', base)
        if not m:
            continue
        name = m.group(1)
        if not any(k.lower() in name.lower() for k in KEYS):
            continue
        d = open(os.path.join(dp, 'body.bin'), 'rb').read()
        cur = out.get(name)
        if cur is None or len(d) > len(cur[0]):
            out[name] = (d, hashlib.sha256(d).hexdigest(), os.path.join(dp, 'body.bin'))
    return out

A, B = collect(ROOTS["Killer"]), collect(ROOTS["K4"])
print("=" * 86)
print("%-26s %-9s %-9s %s" % ("模块", "Killer", "K4", "结论"))
print("=" * 86)
diffmods = []
for n in sorted(set(A) | set(B)):
    ka, kb = A.get(n), B.get(n)
    if not ka or not kb:
        print("%-26s %-9s %-9s 仅单边有" % (n, len(ka[0]) if ka else "-",
                                        len(kb[0]) if kb else "-"))
        continue
    da, db = ka[0], kb[0]
    if da == db:
        verdict = "★ 逐字节完全相同"
    elif len(da) != len(db):
        verdict = "!! 长度不同（真实差异）"
        diffmods.append((n, ka, kb))
    else:
        dd = [i for i in range(len(da)) if da[i] != db[i]]
        allpm1 = all(abs(db[i] - da[i]) == 1 for i in dd)
        verdict = ("仅 TE 重定位（%d 处 ±1）" % len(dd)) if allpm1 else \
                  ("!! 逻辑差异（%d 处）" % len(dd))
        if not allpm1:
            diffmods.append((n, ka, kb))
    print("%-26s %-9d %-9d %s" % (n, len(da), len(db), verdict))

print("\n" + "=" * 86)
print("真正有内容差异的模块，逐处列出")
print("=" * 86)
for n, ka, kb in diffmods:
    da, db = ka[0], kb[0]
    print("\n%s  Killer %d B vs K4 %d B" % (n, len(da), len(db)))
    print("  Killer sha256 %s" % ka[1][:40])
    print("  K4     sha256 %s" % kb[1][:40])
    print("  Killer 路径 %s" % ka[2][:120])
    if len(da) != len(db):
        print("  → 长度相差 %+d B，逐字节比对不适用" % (len(db) - len(da)))
        # 找公共前缀/后缀
        p = 0
        while p < min(len(da), len(db)) and da[p] == db[p]:
            p += 1
        q = 0
        while q < min(len(da), len(db)) - p and da[-1 - q] == db[-1 - q]:
            q += 1
        print("  → 公共前缀 %d B，公共后缀 %d B，差异窗口 Killer[0x%X:0x%X](%d B) / K4[0x%X:0x%X](%d B)"
              % (p, q, p, len(da) - q, len(da) - q - p, p, len(db) - q, len(db) - q - p))
        continue
    dd = [i for i in range(len(da)) if da[i] != db[i]]
    print("  → %d 处不同" % len(dd))
    for i in dd[:40]:
        print("       0x%04X: %02X -> %02X   (delta %+d)" % (i, da[i], db[i], db[i] - da[i]))
    if len(dd) > 40:
        print("       ... 其余 %d 处省略" % (len(dd) - 40))

print("\n" + "=" * 86)
print("「谁读 Setup 变量的这些偏移」——在所有含 Setup GUID 的模块里扫 +0x190/0x191/0x1B1/0x1B2")
print("=" * 86)
WANT = {0x190: 'CPU LLC', 0x191: 'VDDCR_SOC LLC', 0x1B1: 'VDDCR SOC Voltage',
        0x1B2: 'Vcore Offset Voltage', 0x18B: 'Vcore mode', 0x196: 'SOC mode'}
for tag, R in (("Killer", A), ("K4", B)):
    print("\n[%s]" % tag)
    for n in sorted(R):
        d = R[n][0]
        if SETUP_GUID not in d and n != 'Setup':
            continue
        hits = []
        for off, nm in WANT.items():
            # 形如 48 8B 8B xx / 8B xx disp32 —— 粗扫：该偏移作为 32 位小端常量出现
            cnt = d.count(struct.pack('<I', off))
            # 变址形式 [base+off] 常见编码：8B 8? <disp32>
            cnt2 = sum(1 for m in re.finditer(rb'\x8b[\x40-\xbf]', d)
                       if False)
            if cnt:
                hits.append("%s(0x%X) x%d" % (nm, off, cnt))
        if hits:
            print("   %-26s %s" % (n, "  ".join(hits)))
