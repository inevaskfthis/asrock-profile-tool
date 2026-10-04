# -*- coding: utf-8 -*-
"""VarStoreId=0x1（Setup）的偏移覆盖图：
   对每个 question 取 (VarOffset, Size)，算出它实际占用的字节区间，
   再看 0x190 / 0x233 / 0x27A / 0x191 落在谁的区间里（或谁都不落）。"""
import re, collections, os

V = r"C:/Users/Administrator/WorkBuddy/2026-08-31-07-51-46/v580"
FILES = [("Killer 10.50", "kil10.50_setup_orig.bin.0.0.en-US.uefi.ifr.txt"),
         ("K4 10.50",     "k410.50_setup.bin.0.0.en-US.uefi.ifr.txt")]

RE_OFF = re.compile(r'VarOffset:\s*0x([0-9A-Fa-f]+)')
RE_SIZE = re.compile(r'Size:\s*(\d+)')
RE_VS = re.compile(r'VarStoreId:\s*0x([0-9A-Fa-f]+)')
RE_QID = re.compile(r'QuestionId:\s*0x([0-9A-Fa-f]+)')
RE_PROMPT = re.compile(r'Prompt:\s*"([^"]*)"')

WATCH = [0x190, 0x191, 0x1B1, 0x1B2, 0x233, 0x27A, 0x1A6]

for tag, fn in FILES:
    p = os.path.join(V, fn)
    if not os.path.exists(p):
        print("缺失 " + p); continue
    print("=" * 84)
    print("%s   —— VarStoreId 0x1 (Setup) 的字段覆盖图" % tag)
    print("=" * 84)
    fields = []
    for line in open(p, encoding='utf-8', errors='replace'):
        if 'VarStoreId: 0x1,' not in line:
            continue
        mo, ms, mq = RE_OFF.search(line), RE_SIZE.search(line), RE_QID.search(line)
        if not (mo and ms):
            continue
        off = int(mo.group(1), 16); size = int(ms.group(1))
        nbytes = max(1, (size + 7) // 8)
        pm = RE_PROMPT.search(line)
        fields.append((off, nbytes, mq.group(1) if mq else '?',
                       (pm.group(1) if pm else '')[:34]))
    print("  共 %d 条声明，合计覆盖 %d 字节" % (len(fields), sum(f[1] for f in fields)))

    for w in WATCH:
        hits = [(o, n, q, nm) for (o, n, q, nm) in fields if o <= w < o + n]
        exact = [h for h in hits if h[0] == w and h[1] == 1]
        print("\n  +0x%03X :" % w)
        if not hits:
            print("      ** 没有任何字段覆盖它 **")
        for o, n, q, nm in hits:
            kind = "精确 1 字节" if (o == w and n == 1) else \
                   ("!! 被更大字段覆盖（该字段从 +0x%X 起占 %d 字节）" % (o, n))
            print("      +0x%03X size=%d 字节  QId 0x%-5s  %-34s  %s" % (o, n, q, nm, kind))

    # 找重叠字段：可能互相打架的
    print("\n  --- 重叠字段（同一字节被多条 question 覆盖）---")
    cover = collections.defaultdict(list)
    for o, n, q, nm in fields:
        for b in range(o, o + n):
            cover[b].append((o, q))
    ov = {b: v for b, v in cover.items() if len(v) > 1}
    if not ov:
        print("      无")
    else:
        shown = 0
        for b in sorted(ov):
            if shown > 15:
                print("      ... 共 %d 个字节有重叠" % len(ov)); break
            print("      +0x%03X 被 %s 覆盖" % (b, ", ".join("+0x%X(QId0x%s)" % x for x in ov[b])))
            shown += 1
    print()
