"""python tools/decide.py ask <Tn|-> "<question>" "<option> | <trade-off>" "<option> | <trade-off>" --recommend N --why "<why>"; list; close <Dn> "<choice>" [--by "<who>"]"""

import json, os, re, sys, time
sys.stdout.reconfigure(encoding='utf-8', errors='replace')   # a Windows pipe cannot show every letter otherwise

ROOT = os.environ.get('SCRUMDASH_PROJECT') or os.getcwd()
H = os.path.join(ROOT, 'handoffs')
FILE = os.path.join(H, 'DECISIONS.md')
HEADER = ('# Decisions for the user (tools/decide.py writes a block per question; the board shows it as a card with one '
          'button per option; a click is saved and is recorded here as Chosen)\n\n')

def read(p):
    try:
        return open(p, encoding='utf-8').read()
    except OSError:
        return ''

def next_id():
    ids = [int(x) for x in re.findall(r'^## D(\d+)\b', read(FILE), re.M)]
    for l in read(os.path.join(H, 'APPROVALS.jsonl')).splitlines():
        m = re.search(r'"decision":\s*"D(\d+)"', l)
        if m:
            ids.append(int(m.group(1)))
    return 'D' + str(max(ids, default=0) + 1)

def card_name(tid):
    try:
        c = json.load(open(os.path.join(H, 'CARD-NAMES.json'), encoding='utf-8-sig')).get(tid)
        return c[0] if isinstance(c, list) and c else ''
    except (OSError, ValueError):
        return ''

def blocks():
    out = []
    for m in re.finditer(r'^## (D\d+)\s*[—-]\s*([^\n]*)\n(.*?)(?=^## |\Z)', read(FILE), re.M | re.S):
        body = m.group(3)
        out.append({'id': m.group(1), 'title': m.group(2).strip(), 'chosen': bool(re.search(r'^Chosen:', body, re.M)),
                    'options': [l[2:].split(' | ')[0].strip() for l in body.splitlines() if l.startswith('- ')]})
    return out

def ask(tid, question, pairs, rec, why):
    opts = []
    for p in pairs:
        o, _, t = p.partition('|')
        o, t = o.strip(), t.strip()
        if not o or len(o.split()) > 4:
            sys.exit(f'button "{o}" must be 1-4 words')
        opts.append((o, t))
    if len(opts) < 2:
        sys.exit('at least two options')
    if rec and not 1 <= rec <= len(opts):
        sys.exit('--recommend must point at one of the options (1-based)')
    did = next_id()
    name = card_name(tid) if tid != '-' else ''
    title = f'{name} · {question}' if name else question
    block = (f'## {did} — {title}\n' + (f'Ticket: {tid}\n' if tid != '-' else '') + f'Asked: {time.strftime("%Y-%m-%d %H:%M")}\n'
             + ''.join(f'- {o}' + (f' | {t}' if t else '') + '\n' for o, t in opts)
             + (f'Recommended: {rec}' + (f' — {why}' if why else '') + '\n' if rec else '') + '\n')
    text = read(FILE) or HEADER
    tmp = FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text.rstrip('\n') + '\n\n' + block)
    os.replace(tmp, FILE)
    print(f'decision card {did}: {title}')
    for i, (o, t) in enumerate(opts, 1):
        print(f'  {i}. {o}' + (f'  ({t})' if t else '') + ('  <- recommended' if i == rec else ''))

def close(did, choice, by='chat'):
    text = read(FILE)
    m = re.search(r'^## ' + re.escape(did) + r'\b.*?(?=^## |\Z)', text, re.M | re.S)
    if not m:
        sys.exit(f'{did} not found')
    if re.search(r'^Chosen:', m.group(0), re.M):
        print(f'{did} already chosen'); return
    block = m.group(0).rstrip('\n') + f'\nChosen: {choice} ({time.strftime("%d %b %H:%M")}, {"in chat" if by == "chat" else by})\n\n'
    tmp = FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text[:m.start()] + block + text[m.end():])
    os.replace(tmp, FILE)
    with open(os.path.join(H, 'APPROVALS.jsonl'), 'a', encoding='utf-8') as f:
        f.write(json.dumps({'decision': did, 'choice': choice, 'at': time.strftime('%Y-%m-%d %H:%M:%S'), 'started': True, 'by': by}) + '\n')
    print(f'{did} closed: {choice}')

def main(a):
    if not a:
        sys.exit(__doc__)
    if a[0] == 'list':
        for b in blocks():
            if not b['chosen']:
                print(f'{b["id"]}: {b["title"]}  [{" / ".join(b["options"])}]')
        return
    if a[0] == 'close':
        by = 'chat'
        if '--by' in a[:-1]:
            i = a.index('--by'); by, a = a[i + 1], a[:i] + a[i + 2:]
        if len(a) < 3:
            sys.exit('close <Dn> "<what was chosen>" [--by "<who>"]')
        return close(a[1], a[2], by)
    if a[0] == 'ask':
        rec, why, rest = 0, '', []
        i = 1
        while i < len(a):
            if a[i] == '--recommend':
                rec = int(a[i + 1]); i += 2
            elif a[i] == '--why':
                why = a[i + 1]; i += 2
            else:
                rest.append(a[i]); i += 1
        if len(rest) < 4:
            sys.exit('ask <Tn|-> "<question>" "<option | trade-off>" "<option | trade-off>" [...]')
        return ask(rest[0], rest[1], rest[2:], rec, why)
    sys.exit(__doc__)

if __name__ == '__main__':
    main(sys.argv[1:])
