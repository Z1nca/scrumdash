"""python tools/ticket.py new|wait|building|test|works|partly|fails|parked|done|try|name|show|revive Tn ... | check | archive [--days N] [--dry]"""

import json, os, re, shutil, sys, time
sys.stdout.reconfigure(encoding='utf-8', errors='replace')   # a Windows pipe cannot show every letter otherwise

ROOT = os.environ.get('SCRUMDASH_PROJECT') or os.getcwd()
H = os.path.join(ROOT, 'handoffs')
TICKETS_FILE = os.path.join(H, 'tickets', 'TICKETS.md')
ARCHIVE = os.path.join(H, 'tickets', 'TICKETS-DONE.md')
NAMES = os.path.join(H, 'CARD-NAMES.json')
APPROVALS = os.path.join(H, 'APPROVALS.jsonl')
STATUS_RX = re.compile(r'^(## (T\d+)\b[^\n]*?)\s*\(([^()\n]*(?:\([^()\n]*\)[^()\n]*)*)\)\s*$', re.M)


def read(p, default=''):
    try:
        return open(p, encoding='utf-8').read()
    except OSError:
        return default


def jload(p, default):
    try:
        return json.load(open(p, encoding='utf-8-sig'))
    except (OSError, ValueError):
        return default


def jsave(p, data):
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def write_text(p, text):
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='') as f:
        f.write(text)
    os.replace(tmp, p)


def backup(p):
    try:
        shutil.copyfile(p, p + '.before-ticket-' + time.strftime('%Y%m%d-%H%M%S'))
    except OSError:
        pass
    d, base = os.path.dirname(p), os.path.basename(p) + '.before-ticket-'
    olds = sorted(f for f in os.listdir(d) if f.startswith(base))
    for f in olds[:-5]:
        try:
            os.remove(os.path.join(d, f))
        except OSError:
            pass


def date():
    return time.strftime('%d %b')


def stamp():
    return time.strftime('%d %b %H:%M')


def clean(note):
    return ' '.join(str(note).replace('(', '[').replace(')', ']').replace('\n', ' ').split())


def section(text, tid):
    return re.search(r'^## ' + re.escape(tid) + r'\b.*?(?=^## |\Z)', text, re.M | re.S)


def state_of(head):
    import board
    return board.ticket_state(head)


def heading(tid):
    m = section(read(TICKETS_FILE), tid)
    return m.group(0).split('\n', 1)[0] if m else ''


def set_status(tid, status):
    text = read(TICKETS_FILE)
    m = section(text, tid)
    if not m:
        return ''
    head = m.group(0).split('\n', 1)[0]
    sm = STATUS_RX.match(head)
    title = sm.group(1).rstrip() if sm else head.rstrip()
    new_head = f'{title} ({status})'
    backup(TICKETS_FILE)
    write_text(TICKETS_FILE, text[:m.start()] + new_head + m.group(0)[len(head):] + text[m.end():])
    return new_head


def add_try(tid, line):
    text = read(TICKETS_FILE)
    m = section(text, tid)
    if not m:
        return 0
    sec = m.group(0)
    n = len(re.findall(r'^\s*- (?:Try|try) \d+', sec, re.M)) + 1
    entry = f'- Try {n} ({stamp()}): {" ".join(str(line).split())}\n'
    body = sec.rstrip('\n') + '\n'
    if not re.search(r'^\S*Tries:\s*$', body, re.M):
        body += 'Tries:\n'
    body += entry + '\n'
    write_text(TICKETS_FILE, text[:m.start()] + body + text[m.end():])
    return n


def mark_clicks(tid):
    pend = {}
    for l in read(APPROVALS).splitlines():
        try:
            o = json.loads(l)
        except ValueError:
            continue
        key = 'TEST' if o.get('test') == tid else 'BUILD' if o.get('ticket') == tid else None
        if key:
            pend[key] = None if o.get('started') else o
    marked = 0
    with open(APPROVALS, 'a', encoding='utf-8') as f:
        for o in pend.values():
            if o:
                f.write(json.dumps(dict(o, started=True, by='ticket.py'), ensure_ascii=False) + '\n')
                marked += 1
    return marked


def card_name(tid):
    c = jload(NAMES, {}).get(tid)
    return c[0] if isinstance(c, list) and c else ''


def set_name(tid, name, line):
    if len(name.partition(':')[2].split()) > 8:
        sys.exit('card name: at most ~5 words after the area')
    names = jload(NAMES, {})
    names[tid] = [name.strip(), ' '.join(line.split())]
    jsave(NAMES, names)


def revive(tid):
    arch = read(ARCHIVE)
    m = section(arch, tid)
    if not m:
        return False
    sec = re.sub(r'^<!-- archived [^\n]*-->\n', '', m.group(0), flags=re.M)
    backup(ARCHIVE)
    write_text(ARCHIVE, arch[:m.start()] + arch[m.end():])
    q = read(TICKETS_FILE)
    num = int(tid[1:])
    pos = next((mm.start() for mm in re.finditer(r'^## T(\d+)\b', q, re.M) if int(mm.group(1)) > num), len(q))
    backup(TICKETS_FILE)
    write_text(TICKETS_FILE, q[:pos].rstrip('\n') + '\n\n' + sec.rstrip('\n') + '\n\n' + q[pos:])
    return True


def ensure_listed(tid):
    if not section(read(TICKETS_FILE), tid):
        if revive(tid):
            print(f'{tid}: brought back from the archive')
        else:
            sys.exit(f'{tid}: not on the board or in the archive (ticket.py new ... first)')


def archive(days=3, dry=False):
    import board
    q = read(TICKETS_FILE)
    moved, out, pos = [], [], 0
    for m in re.finditer(r'^## (T\d+)\b.*?(?=^## |\Z)', q, re.M | re.S):
        st = board.ticket_state(m.group(0).split('\n', 1)[0][3:])
        age = (time.time() - board.when_ts(st['when'])) / 86400 if st['when'] else 99
        if st['state'] in ('works', 'done', 'parked') and age > days:
            moved.append((m.group(1), st['state'], st['when'], m.group(0)))
            out.append(q[pos:m.start()])
            pos = m.end()
    out.append(q[pos:])
    for tid, st, when, sec in moved:
        print(f'  {tid:5} {st:7} {when or "-":13} {len(sec):6} chars  {sec.split(chr(10), 1)[0][len(tid) + 6:70]}')
    print(f'{"would move" if dry else "moved"} {len(moved)} sections, {sum(len(s) for _, _, _, s in moved)} of {len(q)} chars')
    if dry or not moved:
        return
    backup(TICKETS_FILE)
    backup(ARCHIVE)
    write_text(TICKETS_FILE, re.sub(r'\n{3,}', '\n\n', ''.join(out)))
    arch = read(ARCHIVE).rstrip('\n') + '\n\n' + ''.join(f'<!-- archived {time.strftime("%Y-%m-%d %H:%M")} -->\n{sec.rstrip(chr(10))}\n\n' for _, _, _, sec in moved)
    write_text(ARCHIVE, arch)


def check():
    import board
    names = jload(NAMES, {})
    q = read(TICKETS_FILE)
    problems, long = 0, []
    for m in re.finditer(r'^## (T\d+)\b[^\n]*', q, re.M):
        head, tid = m.group(0)[3:], m.group(1)
        st = board.ticket_state(head)
        c = names.get(tid)
        if st['state'] not in ('wait', 'approved', 'building', 'test', 'tested-partly', 'tested-fails', 'open'):
            continue
        if not (isinstance(c, list) and c):
            print(f'{tid}: no card name (ticket.py name {tid} "<Area>: <what>" "<one line>")')
            problems += 1
        if st['state'] == 'open':
            print(f'{tid}: status not readable: ({st["status"][:60]})')
            problems += 1
        if len(head) > 220:
            long.append(f'{tid} ({len(head)})')
    print(f'{problems} things to fix' if problems else 'all tickets readable')
    if long:
        print(f'cosmetic, fixes itself at the next state change: long headings {", ".join(long)}')


def show(tid):
    head = heading(tid)
    if not head:
        print(f'{tid}: in the archive' if section(read(ARCHIVE), tid) else f'{tid}: unknown')
        return
    st = state_of(head[3:])
    print(f'{tid}  {st["state"]:13} {st["when"] or "-":13} name: {card_name(tid) or "(none)"}')
    print('  ' + head)


def main(a):
    if not a:
        sys.exit(__doc__)
    cmd = a[0]
    opts, rest = {}, []
    i = 1
    while i < len(a):
        if a[i] == '--dry':
            opts['dry'] = True
            i += 1
        elif a[i].startswith('--'):
            opts[a[i][2:]] = a[i + 1] if i + 1 < len(a) else ''
            i += 2
        else:
            rest.append(a[i])
            i += 1
    if cmd == 'check':
        return check()
    if cmd == 'archive':
        return archive(int(opts.get('days', 3)), bool(opts.get('dry')))
    tid = rest[0] if rest else ''
    if not re.fullmatch(r'T\d+', tid):
        sys.exit('ticket id like T7 expected')
    if cmd == 'show':
        return show(tid)
    if cmd == 'revive':
        print('revived' if revive(tid) else 'not in the archive')
        return
    if cmd == 'name':
        if len(rest) < 3:
            sys.exit('name Tn "<Area>: <what>" "<one everyday line>"')
        set_name(tid, rest[1], rest[2])
        print(f'{tid} card name: {rest[1]}')
        return
    if cmd == 'new':
        if len(rest) < 3:
            sys.exit('new Tn "<Area>: <what was asked>" "<one everyday line>"')
        set_name(tid, rest[1], rest[2])
        q = read(TICKETS_FILE)
        if section(q, tid):
            print(f'{tid}: already on the board (name updated)')
        else:
            block = (f'## {tid} — {rest[1].strip()} (waiting for the user\'s yes)\n'
                     + f'**Goal:** {rest[2].strip()}\nTries:\n\n')
            backup(TICKETS_FILE)
            write_text(TICKETS_FILE, q.rstrip('\n') + '\n\n' + block)
            print(f'{tid}: written, waiting for a yes')
        return
    if cmd == 'try':
        if len(rest) < 2:
            sys.exit('try Tn "<what was tried, result>"')
        ensure_listed(tid)
        print(f'{tid}: Try {add_try(tid, rest[1])} logged')
        return
    note = clean(rest[1]) if len(rest) > 1 else ''
    name = card_name(tid)
    ensure_listed(tid)
    if cmd == 'wait':
        status = "waiting for the user's yes" + (f'; {note}' if note else '')
    elif cmd == 'building':
        if not note:
            sys.exit('building Tn "<what is being built>"')
        status = f'Claude building now [{stamp()}]: {note[:90]}'
    elif cmd == 'test':
        checks = [x.strip() for x in note.split('•') if x.strip()] if '•' in note else [note]
        for c in checks:
            if len(c.split()) > 15:
                print(f'WARNING {tid}: a card line has {len(c.split())} words; keep each check to one plain sentence of about 12 words')
        if opts.get('subject'):
            set_name(tid, opts['subject'], note or card_name(tid))
            name = opts['subject']
        else:
            print(f'NOTE {tid}: no --subject; the card keeps "{name}".')
        if not name:
            print(f'WARNING {tid}: no card name yet; the card shows the heading title. Add one: ticket.py name {tid} "<Area>: <what>" "<line>"')
        tries = opts.get('try') or str(len(re.findall(r'^\s*- (?:Try|try) \d+', section(read(TICKETS_FILE), tid).group(0), re.M)) or 1)
        status = f'ready for your test: try {tries}, built {stamp()}' + (f': {note}' if note else '')
    elif cmd == 'works':
        status = f'WORKS {stamp()}' + (f': {note}' if note else '')
    elif cmd == 'partly':
        status = f'tested {date()}: works partly: {note or "see Tries"}; your AI fixes the rest'
    elif cmd == 'fails':
        status = f'tested {date()}: doesn\'t work: {note or "see Tries"}; your AI writes a fix'
    elif cmd == 'parked':
        status = f'PARKED {date()}' + (f': {note}' if note else '')
    elif cmd == 'done':
        status = f'DONE {date()}' + (f': {note}' if note else '')
    else:
        sys.exit(f'unknown command {cmd}\n' + __doc__)
    if cmd in ('works', 'partly', 'fails'):
        add_try(tid, f'user test: {cmd}' + (f': {rest[1]}' if len(rest) > 1 else ''))
    new_head = set_status(tid, status)
    marked = mark_clicks(tid)
    print(new_head)
    if marked:
        print(f'  {marked} board press(es) of {tid} marked handled')


if __name__ == '__main__':
    main(sys.argv[1:])
