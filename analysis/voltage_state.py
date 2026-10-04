# -*- coding: utf-8 -*-
"""把 r1/r2/r3（以及 r4 若已存在）的电压字节一次性摆出来对照。"""
import os

OFFS = [(0x18B, 'CPU Vcore Voltage mode',   'K4 only, Killer=隐藏声明 QId0x25A'),
        (0x18C, 'CPU Vcore Fixed V',        'K4 only'),
        (0x190, 'CPU Load-Line Calibration','K4 only ← 探针1 写的就是这个字节'),
        (0x191, 'VDDCR_SOC LL Calibration', 'Killer 可见 QId 0xF7  ← 用户设成 Lv3'),
        (0x194, 'CPU Vcore Offset V',       'K4 only'),
        (0x196, 'VDDCR_SOC Voltage mode',   'Killer=隐藏声明 QId0x259'),
        (0x197, 'VDDCR_SOC Fixed V',        'K4 only'),
        (0x19B, 'VDDCR_SOC Offset V',       'K4 only'),
        (0x1A4, 'VDDP',                     '可见'), (0x1A6, 'VTT_DDR', '可见'),
        (0x1AA, '1.05V_PROM Voltage',       '可见'),
        (0x1AC, 'CPU VDD 1.8 Voltage',      '可见'),
        (0x1AE, '2.50V_PROM Voltage',       '可见'),
        (0x1B0, 'VPPM',                     '可见'),
        (0x1B1, 'VDDCR SOC Voltage',        '有 question，被 B 支 Suppress'),
        (0x1B2, 'Vcore Offset Voltage',     '有 question，被 B 支 Suppress')]

CANDS = [r"H:/bios_work/r1.bin", r"H:/bios_work/r2.bin", r"H:/bios_work/r3.bin",
         r"H:/bios_work/r4.bin", r"C:/bios_work/probe.bin"]
imgs = {}
for p in CANDS:
    if os.path.exists(p):
        imgs[os.path.basename(p)] = (open(p, 'rb').read(), p)

print("找到 %d 个镜像: %s" % (len(imgs), ", ".join(imgs)))
if not imgs:
    raise SystemExit("没有镜像")

names = list(imgs)
# 动态定位活体 Setup 记录（名字 Setup + payload 0x280 + 偏移最大）
def find_setup(d):
    best = None
    i = 0
    while True:
        i = d.find(b'NVAR', i)
        if i < 0:
            break
        sz = int.from_bytes(d[i + 4:i + 6], 'little')
        e = d.find(b'\x00', i + 11, i + 40)
        if e > 0:
            nm = d[i + 11:e]
            if nm == b'Setup' and sz == 0x291 and (best is None or i > best):
                best = i
        i += 1
    return best

base = None
print()
for n in names:
    d = imgs[n][0]
    rec = find_setup(d)
    if rec is None:
        print("%-10s 找不到活体 Setup 记录（size 0x291）" % n)
        continue
    db = rec + 0x11                       # 数据基址 = NVAR + 4 + 2 + 5 + len("Setup")+1
    print("%-10s 活体 Setup 记录 @0x%06X  数据基址 0x%06X" % (n, rec, db))
    base = db

print()
hdr = "  %-5s %-28s" % ("off", "name")
for n in names:
    hdr += " %-9s" % n
print(hdr)
print("-" * (36 + 10 * len(names)))
for off, nm, note in OFFS:
    row = "  +0x%03X %-28s" % (off, nm)
    vals = []
    for n in names:
        d = imgs[n][0]
        rec = find_setup(d)
        v = d[rec + 0x11 + off] if rec is not None else 0xFF
        vals.append("%02X" % v)
        row += " %-9s" % ("%02X" % v)
    flag = ""
    if len(set(vals)) > 1:
        flag = "   <<< 有变化"
    print(row + flag)

print("\n=== 关键三字节 ===")
for off, nm in ((0x190, 'CPU LLC（探针1 写的；用户从未在 UI 见过它）'),
                (0x191, 'SOC LLC（用户 UI 设成 Lv3 ⇒ 期望 0x03）'),
                (0x1A6, 'VTT_DDR（出厂非零基线）')):
    line = "  +0x%03X  %-42s" % (off, nm)
    for n in names:
        d = imgs[n][0]
        rec = find_setup(d)
        line += "  %s=%02X" % (n.replace('.bin', ''), d[rec + 0x11 + off] if rec else 0xFF)
    print(line)
