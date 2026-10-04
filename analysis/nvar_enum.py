# -*- coding: utf-8 -*-
"""按真实 NVAR 条目格式枚举：GUID(16) + 'NVAR' + u16 size + 5B hdr + name + NUL + data"""
import re, struct, sys, uuid, collections

P = sys.argv[1] if len(sys.argv) > 1 else r"H:/bios_work/r1.bin"
d = open(P, 'rb').read()
SETUP_GUID = "ec87d643-eba4-4bb5-a1e5-3f3e36b20da9"

def guid_at(p):
    b = d[p:p + 16]
    if len(b) < 16:
        return None
    return str(uuid.UUID(bytes_le=b)).lower()

entries = []
for m in re.finditer(rb'NVAR', d):
    p = m.start()
    if p < 16:
        continue
    size = struct.unpack_from('<H', d, p + 4)[0]
    hdr = d[p + 6:p + 11]
    q = p + 11
    e = d.find(b'\x00', q, q + 80)
    if e < 0:
        continue
    nb = d[q:e]
    if not nb or not all(0x20 <= c < 0x7F for c in nb):
        continue
    entries.append(dict(off=p, size=size, hdr=hdr.hex(), guid=guid_at(p - 16),
                        name=nb.decode('ascii'), data=p + 11 + len(nb) + 1))

print("共 %d 条 NVAR 条目" % len(entries))

print("\n=== 名字含 dram（不分大小写）的条目 ===")
hit = [e for e in entries if re.search(r'dram', e['name'], re.I)]
if hit:
    for e in hit:
        print("  %s" % e)
else:
    print("  ** 无 **")

print("\n=== GUID == Setup GUID 的全部条目 ===")
sg = [e for e in entries if e['guid'] == SETUP_GUID]
print("  共 %d 条" % len(sg))
cnt = collections.Counter(e['name'] for e in sg)
for nm, c in sorted(cnt.items()):
    ex = next(e for e in sg if e['name'] == nm)
    print("  %-28s x%-2d  size 0x%-4X  首条 NVAR@0x%06X  data@0x%06X" % (
        nm, c, ex['size'], ex['off'], ex['data']))

print("\n=== 所有不同 GUID ===")
gc = collections.Counter(e['guid'] for e in entries)
for g, c in gc.most_common():
    nm = sorted({e['name'] for e in entries if e['guid'] == g})
    print("  %s  x%-3d  %s" % (g, c, ", ".join(nm[:6]) + (" …" if len(nm) > 6 else "")))

print("\n=== 全部不同变量名 (%d) ===" % len({e['name'] for e in entries}))
print("  " + ", ".join(sorted({e['name'] for e in entries})))
