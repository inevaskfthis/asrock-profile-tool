# -*- coding: utf-8 -*-
"""在所有解包模块的 body.bin 里搜 ASCII 'DRAM\0'，并报告该模块是否含 Setup GUID。"""
import os, re

ROOTS = [r"C:/bios_work/verify_stock/stock.bin.dump",
         r"C:/bios_work/verify_k4/k4.bin.dump"]
GUID = bytes.fromhex("43D687ECA4EBB54BA1E53F3E36B20DA9")

for root in ROOTS:
    print("=" * 78)
    print(root)
    print("=" * 78)
    n = 0
    for dp, dn, fn in os.walk(root):
        if 'body.bin' not in fn:
            continue
        p = os.path.join(dp, 'body.bin')
        try:
            d = open(p, 'rb').read()
        except Exception:
            continue
        n += 1
        if b'DRAM\x00' not in d:
            continue
        mod = dp.split(os.sep)[-2] if 'image section' in dp else dp.split(os.sep)[-1]
        has_guid = GUID in d
        # 上下文
        ctxs = []
        for m in re.finditer(rb'DRAM\x00', d):
            q = m.start()
            ctxs.append((q, d[max(0, q - 8):q + 12]))
        print("  [%s] %s" % ("含SetupGUID" if has_guid else "  --  ", dp.split(root)[1][:110]))
        for q, c in ctxs[:6]:
            print("         @0x%06X  %s  |%s|" % (q, c.hex(' '),
                  re.sub(rb'[^\x20-\x7E]', b'.', c).decode()))
    print("  扫描 body.bin 共 %d 个" % n)
    print()
