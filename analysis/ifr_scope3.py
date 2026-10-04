# -*- coding: utf-8 -*-
"""IFR 门控链解析器 v3 —— 按缩进重建作用域栈。
IFRExtractor 输出里，行首 tab 数量 == IFR scope 深度。
遇到 scope 开启行（SuppressIf/GrayOutIf/Form/OneOf/Numeric/...）就把栈里 indent >= 当前的行弹掉再压入；
遇到 question 行，栈中所有仍在的 SuppressIf/GrayOutIf 就是它的门控链。
这样 eq/and/or 那套嵌套作用域不会干扰。"""
import re, sys

RE_LINE   = re.compile(r'^(?:(0x[0-9A-Fa-f]+):\s?)?(\t*)(.*)$')
RE_PROMPT = re.compile(r'\b(OneOf|Numeric|CheckBox|Text|Password|OrderedList)\s+Prompt:\s*"([^"]*)"')
RE_VOFF   = re.compile(r'VarOffset:\s*0x([0-9A-Fa-f]+)')
RE_QID    = re.compile(r'QuestionId:\s*0x([0-9A-Fa-f]+)')
RE_VSTORE = re.compile(r'VarStoreId:\s*0x([0-9A-Fa-f]+)')
RE_EQ     = re.compile(r'^EqIdVal\s+QuestionId:\s*0x([0-9A-Fa-f]+),\s*Value:\s*0x([0-9A-Fa-f]+)')
RE_NEQ    = re.compile(r'^EqIdVal\s+QuestionId:\s*0x([0-9A-Fa-f]+),\s*Value:\s*0x([0-9A-Fa-f]+)')
RE_FORMID = re.compile(r'FormId:\s*0x([0-9A-Fa-f]+)')
RE_TITLE  = re.compile(r'Title:\s*"([^"]*)"')

COND = {'SuppressIf', 'GrayOutIf', 'InconsistentIf', 'DisableIf', 'WarningIf'}
SCOPE = COND | {'Form', 'FormSet', 'OneOf', 'Numeric', 'CheckBox', 'Text', 'Password',
                'OrderedList', 'Subtitle', 'Ref', 'EqIdVal', 'Or', 'And', 'Not'}
NOSCOPE = {'Action', 'ResetButton', 'Label', 'Goto', 'End', 'SuppressIf'} - {'SuppressIf'}

def parse(path):
    lines = open(path, encoding='utf-8', errors='replace').read().split('\n')
    stack = []      # (indent, kind, cond_text)
    forms = {}
    cur_form = None
    for l in lines:
        m = RE_LINE.match(l)
        if not m:
            continue
        body = m.group(3)
        if not body.strip():
            continue
        indent = len(m.group(2))
        s = body.strip()
        head = re.match(r'^([A-Za-z]+)\b', s)
        if not head:
            continue
        kind = head.group(1)

        if kind == 'End':
            continue
        if kind == 'Form':
            fm = RE_FORMID.search(s)
            ti = RE_TITLE.search(s)
            fid = int(fm.group(1), 16) if fm else None
            cur_form = fid
            forms.setdefault(fid, {'title': ti.group(1) if ti else '', 'items': []})
            stack = []
            continue
        if kind == 'FormSet':
            continue

        # 弹栈：所有 indent >= 当前 的作用域都已结束
        while stack and stack[-1][0] >= indent:
            stack.pop()

        opens = kind in SCOPE
        if kind == 'EqIdVal':
            em = RE_EQ.match(s)
            if em:
                q, v = int(em.group(1), 16), int(em.group(2), 16)
                # 回填到最近的、还没有条件的 COND 祖先
                tgt = next((f for f in reversed(stack)
                            if f[1] in COND and f[2] is None), None)
                if tgt is not None:
                    tgt[2] = (q, v)
                else:
                    tgt2 = next((f for f in reversed(stack) if f[1] in COND), None)
                    if tgt2 is not None:
                        tgt2[3].append((q, v))
            stack.append([indent, kind, None, []])
            continue
        if kind in ('Or', 'And', 'Not'):
            stack.append([indent, kind, None, []])
            continue

        def record():
            pm = RE_PROMPT.search(s)
            vm = RE_VOFF.search(s)
            if not (pm and vm) or cur_form is None or cur_form not in forms:
                return
            gates = []
            for fr in stack:
                if fr[1] in COND:
                    gates.append((fr[1], fr[2], list(fr[3])))
            qm = RE_QID.search(s)
            vst = RE_VSTORE.search(s)
            forms[cur_form]['items'].append({
                'off': int(vm.group(1), 16), 'name': pm.group(2),
                'qid': qm.group(1) if qm else '?',
                'vstore': vst.group(1) if vst else '?',
                'gates': gates})

        if opens:
            record()
            stack.append([indent, kind, None, []])
            continue
        record()
        continue

    return forms

V = r"C:/Users/Administrator/WorkBuddy/2026-08-31-07-51-46/v580"
WANT = {0x191, 0x1B0, 0x1B1, 0x1B2, 0x1A4, 0x1A6, 0x1AA, 0x1AC, 0x1AE,
        0x18B, 0x18C, 0x190, 0x194, 0x196, 0x197, 0x19B}
NAME = {0x254: 'DRAM+0x56', 0x255: 'DRAM+0x57', 0x204: 'SystemAccess+0x00',
        0x206: 'SystemAccess+0x00', 0x256: '?', 0x2808: 'CPU Vcore Voltage'}

def report(path, tag):
    forms = parse(path)
    print("#" * 78)
    print("#  %s" % tag)
    print("#" * 78)
    for fid, f in sorted(forms.items()):
        vs = [i for i in f['items'] if i['off'] in WANT]
        if not vs:
            continue
        print("Form 0x%X  %r   —— %d 项，电压项 %d" % (fid, f['title'], len(f['items']), len(vs)))
        for it in vs:
            print("   +0x%03X  %-40s QId 0x%-5s" % (it['off'], it['name'][:40], it['qid']))
            for k, c, extra in it['gates']:
                if c:
                    tail = ""
                    if extra:
                        tail = "  OR " + "  ".join("QId 0x%X == %d [%s]" % (
                            e[0], e[1], NAME.get(e[0], '?')) for e in extra)
                    print("         %-14s QId 0x%X == %d   [%s]%s" % (
                        k, c[0], c[1], NAME.get(c[0], '?'), tail))
                else:
                    print("         %-14s (条件未捕获)" % k)
        print()
    print()

report(V + "/kil10.50_setup_orig.bin.0.0.en-US.uefi.ifr.txt", "Killer 10.50")
report(V + "/k410.50_setup.bin.0.0.en-US.uefi.ifr.txt", "K4 10.50")
