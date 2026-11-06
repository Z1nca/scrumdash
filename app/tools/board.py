import base64, glob, hashlib, hmac, html, json, os, re, runpy, secrets, subprocess, sys, threading, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ticket_notes

TOOLS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get('SCRUMDASH_PROJECT') or os.getcwd()
E = html.escape


def P(*parts):
    return os.path.join(ROOT, 'handoffs', *parts)


TICKETS_FILE = P('tickets', 'TICKETS.md')
DONE_FILE = P('tickets', 'TICKETS-DONE.md')
DENIED_FILE = P('tickets', 'TICKETS-DENIED.md')
APPROVALS = P('APPROVALS.jsonl')
DECISIONS = P('DECISIONS.md')
IMG_DIR, IMG_PARTS, IMG_LOCK = P('user-images'), {}, threading.Lock()
CLAUDE_STEPS = ['Reading', 'Building', 'Checking', 'Your test']


def read(path, default=''):
    try:
        return open(path, encoding='utf-8').read()
    except OSError:
        return default


def save(path, text):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)


def json_file(name):
    try:
        d = json.loads(read(P(name)) or '{}')
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def record(**fields):
    with open(APPROVALS, 'a', encoding='utf-8') as f:
        f.write(json.dumps({**fields, 'at': time.strftime('%Y-%m-%d %H:%M:%S')}, ensure_ascii=False) + '\n')


def _secret():
    p = os.path.expanduser('~/.scrumdash-secret')
    try:
        return open(p, 'rb').read()
    except OSError:
        s = secrets.token_bytes(32)
        open(p, 'wb').write(s)
        return s


_SECRET = _secret()


def sign(what):
    return hmac.new(_SECRET, ('approve:' + what).encode(), 'sha256').hexdigest()[:24]


def signed(key, what):
    return hmac.compare_digest(key.encode(), sign(what).encode())


_BUILD_YES = [None, set()]


def build_yes():
    try:
        st = os.stat(APPROVALS)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if key is not None and _BUILD_YES[0] == key:
        return _BUILD_YES[1]
    pending = set()
    for line in read(APPROVALS).splitlines():
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get('ticket'):
            pending = pending - {o['ticket']} if o.get('started') else pending | {o['ticket']}
    _BUILD_YES[:] = [key, pending]
    return pending


_STATUS_RX = re.compile(r'\s*\(([^()\n]*(?:\([^()\n]*\)[^()\n]*)*)\)\s*$')


def heading_status(head):
    m = _STATUS_RX.search(head)
    if m:
        return m.group(1)
    i = head.find(' (')
    return head[i + 2:].rstrip(')') if i > 0 else ''


def ticket_state(head):
    tid = head.split()[0]
    status = heading_status(head)
    low = status.lower().strip()
    first = low.split()[0] if low else ''
    if 'DONE' in head or low.startswith(('already works', 'done')):
        state = 'done'
    elif first == 'parked':
        state = 'parked'
    elif first == 'works' or re.match(r'tested[^:;]*:\s*works(?!\s+partly)', low):
        state = 'works'
    elif low.startswith('tested') and ('partly' in low.split(';')[0] or 'feedback given' in low.split(';')[0]):
        state = 'tested-partly'
    elif low.startswith('tested'):
        state = 'tested-fails'
    elif low.startswith('ready for your test'):
        state = 'test'
    elif 'building now' in low or first == 'building':
        state = 'building'
    elif 'user-approved' in low or tid in build_yes():
        state = 'approved'
    elif 'waiting for the user' in low:
        state = 'wait'
    else:
        state = 'open'
    when = re.search(r'\b(\d{1,2} [A-Z][a-z]{2}(?: \d\d:\d\d)?)', status)
    tried = re.search(r'\btry (\d+)', low)
    return {'tid': tid, 'state': state, 'status': status, 'when': when.group(1) if when else '', 'try': int(tried.group(1)) if tried else 0}


def when_ts(when):
    if not when:
        return 0
    try:
        t = time.mktime(time.strptime(f'{time.localtime().tm_year} {when}', '%Y %d %b %H:%M' if ':' in when else '%Y %d %b'))
        return t - 366 * 86400 if t > time.time() + 86400 else t
    except ValueError:
        return 0


def asked_ts(s):
    s = str(s or '').strip()
    if not s:
        return 0
    try:
        return time.mktime(time.strptime(s[:16], '%Y-%m-%d %H:%M'))
    except ValueError:
        return when_ts(s)


def rel_age(ts):
    if not ts:
        return ''
    sec = max(0, time.time() - ts)
    return f'{int(sec // 60)} min' if sec < 3600 else f'{int(sec // 3600)} h' if sec < 86400 else f'{int(sec // 86400)} d'


def tickets():
    return re.findall(r'^## (T\d+[^\n]*)', read(TICKETS_FILE), re.M)


def section_of(tid, text):
    return re.search(r'^## ' + re.escape(tid) + r'\b.*?(?=^## |\Z)', text, re.M | re.S)


def waiting(tid):
    return any(h.split()[0] == tid and ticket_state(h)['state'] == 'wait' for h in tickets())


def card_names():
    try:
        return json.loads(read(P('CARD-NAMES.json')) or '{}')
    except ValueError:
        return {}


def plain_title(head):
    tid, _, rest = head.partition(' ')
    c = card_names().get(tid)
    if isinstance(c, list) and c:
        return c[0]
    rest = rest.lstrip('—- ').strip()
    rest = re.sub(r'\s*\((?:waiting for|user-approved|built|ready for|tested|done)[^)]*\)\s*$', '', rest, flags=re.I)
    return re.sub(r':\s*DONE.*$', '', rest)


def title_of(tid, fallback):
    return next((plain_title(h) for h in tickets() if h.split()[0] == tid), fallback)


def short_name(head, n=58):
    t = plain_title(head)
    return t if len(t) <= n else t[:n].rsplit(' ', 1)[0] + '…'


def plain(text, limit=130):
    t = re.sub(r'`[^`]*`', '', text)
    t = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', t)
    t = re.sub(r'\S*\\\S+|\S+/\S+\.\w+', '', t)
    t = re.sub(r'[*_#>]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    parts = re.split(r'(?<=[.!?:])\s', t, maxsplit=2)
    s = parts[0] + (' ' + parts[1] if parts[0].endswith(':') and len(parts) > 1 else '')
    return s if len(s) <= limit else s[:limit].rsplit(' ', 1)[0] + '…'


def goal_line(tid):
    c = card_names().get(tid)
    if isinstance(c, list) and len(c) > 1:
        return c[1]
    sec = section_of(tid, read(TICKETS_FILE))
    m = re.search(r'\*\*Goal[^*]*:\*\*\s*([^\n]*)', sec.group(0)) if sec else None
    return plain(m.group(1), 160) if m else ''


def mark_approved(tid):
    q = read(TICKETS_FILE)
    new = re.sub(r'^(## ' + re.escape(tid) + r'\b[^\n]*?)\(waiting for the user\'s yes\)', lambda m: m.group(1) + '(user-approved ' + time.strftime('%d %b') + '; next up)', q, count=1, flags=re.M)
    if new != q:
        save(TICKETS_FILE, new)


def mark_tested(tid, result):
    label = 'tested ' + time.strftime('%d %b') + ': ' + {'works': 'works', 'feedback': 'feedback given in chat, your AI follows up', 'partly': 'works partly, your AI fixes the rest'}.get(result, "doesn't work, your AI writes a fix")
    q = read(TICKETS_FILE)
    new = re.sub(r'^(## ' + re.escape(tid) + r'\b[^\n]*?)\([^()\n]*ready for your test[^\n]*$', lambda m: m.group(1) + '(' + label + ')', q, count=1, flags=re.M)
    if new != q:
        save(TICKETS_FILE, new)


def deny_ticket(tid):
    q = read(TICKETS_FILE)
    m = section_of(tid, q)
    if not m:
        return
    with open(DENIED_FILE, 'a', encoding='utf-8') as f:
        f.write(('# Denied tickets\n\n' if not os.path.exists(DENIED_FILE) else '') + f'<!-- denied {time.strftime("%Y-%m-%d %H:%M")} -->\n' + m.group(0).rstrip() + '\n\n')
    save(TICKETS_FILE, q[:m.start()] + q[m.end():])


def already_works(tid):
    q = read(TICKETS_FILE)
    m = section_of(tid, q)
    if not m:
        return
    with open(DONE_FILE, 'a', encoding='utf-8') as f:
        f.write('\n' + m.group(0).rstrip().replace("(waiting for the user's yes)", '(ALREADY WORKS per the user, ' + time.strftime('%d %b') + ')', 1) + '\n')
    save(TICKETS_FILE, q[:m.start()] + q[m.end():])


def decisions():
    out = []
    for m in re.finditer(r'^## (D\d+)\s*[—-]\s*([^\n]*)\n(.*?)(?=^## |\Z)', read(DECISIONS), re.M | re.S):
        did, title, body = m.group(1), m.group(2).strip(), m.group(3)
        opts, offs = [], []
        for l in body.splitlines():
            if l.startswith('- '):
                o, _, t = l[2:].partition(' | ')
                opts.append(o.strip())
                offs.append(t.strip())
        if not opts:
            continue
        def field(key):
            f = re.search(r'^' + key + r':\s*(.*)$', body, re.M)
            return f.group(1).strip() if f else ''
        rec = re.match(r'(\d+)\s*(?:[—-]\s*(.*))?', field('Recommended'))
        out.append({'id': did, 'title': title, 'options': opts, 'tradeoffs': offs, 'ticket': field('Ticket'), 'asked': field('Asked'),
                    'rec': int(rec.group(1)) if rec and 0 < int(rec.group(1)) <= len(opts) else 0, 'why': (rec.group(2) or '').strip() if rec else '',
                    'chosen': field('Chosen')})
    return out


def decision_record(did, choice):
    text = read(DECISIONS)
    m = section_of(did, text)
    if not m or re.search(r'^Chosen:', m.group(0), re.M):
        return
    save(DECISIONS, text[:m.start()] + m.group(0).rstrip('\n') + f'\nChosen: {choice} ({time.strftime("%d %b %H:%M")})\n\n' + text[m.end():])


def chosen():
    got = {}
    for line in read(APPROVALS).splitlines():
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get('decision') and o.get('choice'):
            got[o['decision']] = o['choice']
    return got


def ideas_md():
    out = {'also': [], 'instead': [], 'later': [], 'done': []}
    for l in read(P('IDEAS.md')).splitlines():
        if l.startswith('#') or l.count('|') < 2:
            continue
        date, tag, text = [x.strip() for x in l.split('|', 2)]
        tag = tag.lower()
        state = ''
        m = re.search(r'\s\|\s*((?:built|in test|open|parked)[^|]*)$', text)
        if m:
            state, text = m.group(1).strip(), text[:m.start()].strip()
        if tag in out and text:
            out['done' if re.match(r'(built|in test|rule)', state) else tag].append((date, text + (f' [{state}]' if state else '')))
    return {k: v[::-1] for k, v in out.items()}


def ideas_html():
    md = ideas_md()
    n = sum(len(v) for k, v in md.items() if k != 'done')
    one = lambda d, t: ('Idea from my board (' + d + '): ' + t + chr(10) + 'Check whether it is already built or partly built. '
                        'If not, make it a ticket in my own words and tell me what you need from me.')
    copy = ('const t=this.dataset.p;(navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).then(()=&gt;{this.textContent=&#39;Copied&#39;},'
            '()=&gt;{const a=document.createElement(&#39;textarea&#39;);a.value=t;document.body.appendChild(a);a.select();document.execCommand(&#39;copy&#39;);a.remove();this.textContent=&#39;Copied&#39;})')
    line = lambda d, t: (f'<div class="okrow"><div class="ts">{E(d)}</div><div style="font-size:14px">{E(t)}</div>'
                         f'<button class="b2" type="button" style="margin-top:4px;padding:3px 10px;font-size:12px" data-p="{E(one(d, t))}" onclick="{copy}">Prompt</button></div>')
    groups = ''
    for tag, name in (('also', 'Added'), ('instead', 'Changed direction'), ('later', 'Parked')):
        if md[tag]:
            groups += f'<div class="grp">{name} ({len(md[tag])})</div>' + ''.join(line(d, t) for d, t in md[tag])
    if md['done']:
        groups += (f'<details class="tc-more"><summary class="grp" style="text-align:left">Built or in test ({len(md["done"])})</summary>'
                   + ''.join(line(d, t) for d, t in md['done']) + '</details>')
    words = {'also': 'added', 'instead': 'change of direction', 'later': 'parked'}
    items = [f'- {d} | {words[tag]} | {t}' for tag in ('also', 'instead', 'later') for d, t in md[tag]]
    prompt = ('These are all my ideas from the board for this project (' + str(len(items)) + '). For each one: say '
              'whether it is already built, partly built or not built yet; merge duplicates; then turn what is left into tickets waiting '
              'for my yes, in a sensible order, each in my own words. "change of direction" replaced an earlier idea; "parked" waits '
              'until I bring it back.' + chr(10) * 2 + chr(10).join(items)) if items else ''
    maker = (f'<details class="tc-more" style="margin:2px 0 8px"><summary class="b2" style="text-align:center">📝 Make prompt ({len(items)} ideas)</summary>'
             f'<textarea id="ideaprompt" readonly rows="8" style="width:100%;box-sizing:border-box;font:12px monospace;background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:6px;margin-top:6px">{E(prompt)}</textarea>'
             '<button class="b2" type="button" style="width:100%;margin-top:6px" onclick="const t=document.getElementById(&#39;ideaprompt&#39;);'
             't.select();(navigator.clipboard?navigator.clipboard.writeText(t.value):Promise.reject()).then(()=&gt;{this.textContent=&#39;Copied&#39;},()=&gt;{document.execCommand(&#39;copy&#39;);this.textContent=&#39;Copied&#39;})">Copy prompt</button></details>') if items else ''
    return n, (f'<details class="sec foldsec" open><summary><h2>💬 Ideas{f" ({n})" if n else ""}</h2></summary>'
               ''
               + maker + (groups or '<div class=ts>No ideas yet.</div>') + '</details>')


def open_questions():
    out = []
    for l in read(P('OPEN-QUESTIONS.md')).splitlines():
        if l.startswith('#') or l.count('|') < 3:
            continue
        date, asker, q, ans = [x.strip() for x in l.split('|', 3)]
        if q and not ans.lower().startswith(('yes', 'answered')) and ans.lower() != 'y':
            out.append({'date': date, 'asker': asker, 'q': q, 'key': hashlib.sha1(q.encode('utf-8')).hexdigest()[:12]})
    return out[::-1]


def mark_question_answered(key):
    path = P('OPEN-QUESTIONS.md')
    lines = read(path).splitlines()
    hit = False
    for i, l in enumerate(lines):
        if l.startswith('#') or l.count('|') < 3:
            continue
        q = [x.strip() for x in l.split('|', 3)][2]
        if hashlib.sha1(q.encode('utf-8')).hexdigest()[:12] == key:
            lines[i] = l.rsplit('|', 1)[0].rstrip() + f' | yes ({time.strftime("%d %b %H:%M")}, board)'
            hit = True
    if hit:
        save(path, '\n'.join(lines) + '\n')


def with_image(form, text):
    iid = form.get('img', [''])[0]
    if re.fullmatch(r'[0-9a-f]{12}', iid) and os.path.exists(os.path.join(IMG_DIR, iid + '.jpg')):
        return (text + ' ' if text else '') + '[image: handoffs/user-images/' + iid + '.jpg]'
    return text


def link(href, label):
    return f'<a class="b2" href="{href}">{label}</a>'


def note_button(tid, label, cls='b2'):
    return f' <a class="{cls}" href="/note?t={urllib.parse.quote(tid)}&k={sign("note:" + tid)}" onclick="event.stopPropagation()">{label}</a>'


def tc_more(inner):
    return f'<details class="tc-more"><summary>More</summary><div class="tc-row">{inner}</div></details>'


def goal_html(goal):
    items = [x.strip() for x in (goal or '').split('•') if x.strip()]
    if len(items) <= 1:
        return f'<div class="ts tc-g">{E(goal)}</div>' if goal else ''
    return '<ul class="ts tc-list">' + ''.join(f'<li>{E(it)}</li>' for it in items) + '</ul>'


def card_html(key, name, meta, goal, body, kind='', finished=False):
    head = f'<b>{E(name)}</b>'
    tint = next((k for k in kind.split() if k in ('dk', 'yk', 'qk')), '')
    if tint == 'qk':
        head = f'<span class="dk-area">{E(name)}</span>'
    elif tint and ': ' in name:
        area, sub = name.split(': ', 1)
        head = f'<span class="dk-area">{E(area)}</span><b>{E(sub)}</b>'
    return (f'<div class="tc{(" " + kind) if kind else ""}{" hl-claude" if finished else ""}" data-card="{E(key)}">'
            + ('<div class="hl-tag">✓ Claude done · your turn</div>' if finished else '') + '<div class="tc-h"'
            + (f' title="{E(meta)}"' if meta else '') + f'>{head}</div>' + goal_html(goal) + body + '</div>')


def test_row(tid):
    result = lambda r, label: link(f'/test?t={tid}&r={r}&k={sign("test:" + tid + ":" + r)}', label)
    return ('<div class="tc-row n3">' + result('works', 'Works') + result('partly', 'Partly') + result('fails', 'Doesn&#39;t work') + '</div>'
            + '<div class="tc-row">' + result('feedback', 'Feedback provided') + '</div>' + tc_more(note_button(tid, 'Ask about this')))


def approve_row(tid):
    t = urllib.parse.quote(tid)
    return ('<div class="tc-row">' + link(f'/approve?t={t}&k={sign("build:" + tid)}', 'Build it')
            + link(f'/notnow?t={t}&k={sign("notnow:" + tid)}', 'Not now') + '</div>'
            + tc_more(note_button(tid, 'Ask about this') + link(f'/already?t={t}&k={sign("already:" + tid)}', 'Already works')
                      + link(f'/deny?t={t}&k={sign("deny:" + tid)}', 'Drop it')))


def short_question(q):
    parts = [p for p in re.split(r'(?<=[.?!])\s+', q.strip()) if p]
    return next((p for p in parts if p.endswith('?')), parts[0] if parts else q)


def decision_rows(d, live):
    did = d['id']
    question = d['title'].partition(' · ')[2] or d['title']
    head = short_question(question)
    href = lambda i: f'/decide?d={did}&o={i}&k={sign("dec:" + did + ":" + str(i))}'
    rec = d['rec'] - 1 if d['rec'] else -1
    order = ([rec] if rec >= 0 else []) + [i for i in range(len(d['options'])) if i != rec]
    go = f'<a class="dk-go" href="{href(rec)}"><small>★ My pick</small>{E(d["options"][rec])}</a>' if rec >= 0 else ''
    alts = ''.join(f'<a class="dk-alt" href="{href(i)}">{E(d["options"][i])}</a>' for i in order if i != rec)
    alts += note_button(d['ticket'] if d['ticket'] in live else 'new', '✎ Something else', 'dk-alt dk-else')
    rest = ' '.join(question.replace(head, ' ').split())
    info = ((f'<p>{E(rest)}</p>' if rest else '')
            + ''.join(f'<p><b>{"★ " if i == rec else ""}{E(d["options"][i])}</b>{" — " + E(d["tradeoffs"][i]) if d["tradeoffs"][i] else ""}</p>'
                      for i in order if d['tradeoffs'][i])
            + (f'<p class="dk-why">Why my pick: {E(d["why"])}</p>' if rec >= 0 and d['why'] else ''))
    return (f'<div class="tc-q dk-q">{E(head)}</div>{go}<div class="dk-alts">{alts}</div>'
            + (f'<details class="dk-more"><summary>Details</summary>{info}</details>' if info else ''))


def all_cards(heads):
    """The Decide cards and the test cards, newest first. A test card comes with the day its ticket was built."""
    live = {h.split()[0]: h for h in heads}
    progress = json_file('CLAUDE-PROGRESS.json')
    finished = progress.get('ticket') if str(progress.get('step', '')) == '3' else None
    taken = chosen()
    decs = {}
    for d in decisions():
        if d['id'] not in taken and not d['chosen']:
            decs.setdefault(d['ticket'] if d['ticket'] in live else '', []).append(d)
    cards = []
    for tid, h in live.items():
        st = ticket_state(h)
        goal = goal_line(tid)
        rows = []
        if st['state'] == 'test':
            rows.append((0, 'test', test_row(tid)))
            note = re.search(r'built \d{1,2} [A-Z][a-z]{2} \d\d:\d\d:\s*(.+)$', st['status'])
            if note:
                goal = note.group(1).strip()
        if st['state'] == 'wait' and tid in decs:
            rows.append((0, 'wait', approve_row(tid)))
        for d in decs.get(tid, []):
            rows.append((asked_ts(d['asked']), 'dec', decision_rows(d, live)))
        if not rows:
            continue
        ts = max(r[0] for r in rows)
        rows.sort(key=lambda r: (r[1] != 'test', -r[0]))
        body = rows[0][2]
        if len(rows) > 1:
            body += f'<details class="then"><summary>Then ({len(rows) - 1})</summary>' + ''.join(r[2] for r in rows[1:]) + '</details>'
        age = rel_age(ts)
        meta = ' · '.join(x for x in (tid, f'try {st["try"]}' if st['try'] else '', f'{age} ago' if age else '') if x)
        first = rows[0][1]
        cards.append((ts, card_html(tid, plain_title(h), meta, '' if first == 'dec' else goal, body, {'dec': 'dk', 'wait': 'yk'}.get(first, ''), tid == finished),
                      when_ts(st['when']) if first == 'test' else None))
    for d in decs.get('', []):
        subject = d['title'].partition(' · ')[0] if ' · ' in d['title'] else ((card_names().get(d['ticket']) or [''])[0] or 'Your choice')
        cards.append((asked_ts(d['asked']), card_html(d['id'], subject, '', '', decision_rows(d, live), 'dk'), None))
    for o in open_questions():
        cards.append((asked_ts(o['date']), card_html('q:' + o['key'], f'{o["asker"] or "Claude"} asked', o['date'], '',
                                                     f'<div class="tc-q dk-q">{E(o["q"])}</div><div class="tc-row">'
                                                     + link(f'/answered?h={o["key"]}&k={sign("answered:" + o["key"])}', 'Answered') + note_button('new', 'Reply') + '</div>', 'qk'), None))
    cards.sort(key=lambda c: c[0], reverse=True)
    build_cards = [card_html('w:' + tid, plain_title(h), tid, goal_line(tid), approve_row(tid), 'small yk')
                   for tid, h in live.items() if ticket_state(h)['state'] == 'wait' and tid not in decs]
    return [c[1] for c in cards if c[2] is None] + build_cards[::-1], [(c[2], c[1]) for c in cards if c[2] is not None]


TAB_HELP = {'now': 'Decide: choices your AI needs from you. Press the big button, or just answer in chat.',
            'test': 'Test: everything ready for you to try; the last 24 hours first. Press Works, Partly or Doesn\u2019t work.',
            'ideas': 'Ideas: everything you mentioned that is not built yet.',
            'more': 'Guide: what every button means. Click a part to open it.',
            'chat': 'Chat: when to compact, when to start a new chat, and the board\u2019s style.'}


def has_info(heads):
    return bool(heads) or any(read(P(n)).strip(' {}\r\n') for n in ('DECISIONS.md', 'IDEAS.md', 'OPEN-QUESTIONS.md', 'CLAUDE-PROGRESS.json', 'NOTES.md'))


def board_html(heads):
    decide, tests = all_cards(heads)
    day = time.time() - 86400
    recent = [h for when, h in tests if when > day]
    older = [h for when, h in tests if when <= day]
    group = lambda title, part: f'<div class="grp">{title}</div>' + ''.join(part) if part and recent and older else ''.join(part)
    test_cards = group('New: last 24 hours', recent) + group('Older', older)   # one Test tab (user 6 Nov: Recent removed, as on the main board)
    nothing = ('<div class="empty">✓ Nothing to decide right now.<div class="ts">Questions, choices and approvals appear here by themselves.</div></div>' if has_info(heads) else
               '<div class="empty">No new information yet<div class="ts">Start working in your chat. Cards and progress appear here by themselves.</div>'
               '<div class="ts"><a href="#" onclick="showTab(\'more\',true);return false">New here? Open the Guide</a></div><div class="ts" style="margin-top:6px;font-weight:600">Happy chatting! 💬</div></div>')
    n_ideas, ideas = ideas_html()
    tab = lambda key, label, n=0: f'<button class="tab" data-t="{key}">{label}{f"<i>{n}</i>" if n else ""}</button>'
    def pane(key, inner, shown=False):
        help_line = f'<div class="tabhelp">{E(TAB_HELP[key])}</div>' if key in ('more', 'chat') or 'class="tc' not in inner else ''
        return f'<section class="pane" data-t="{key}"{"" if shown else " hidden"}>{help_line}{inner}</section>'
    name = os.path.basename(os.path.abspath(ROOT))
    return ('<div class="main m2 board">'
            f'<div class="brand">{LOGO}<b>Scrumdash</b><span>{E(name)}</span></div><div id="chatline" data-k="{sign("chatset")}" hidden></div>'
            f'<nav class="tabs">{tab("now", "Decide", len(decide))}{tab("test", "Test", len(tests))}{tab("ideas", "Ideas", n_ideas)}{tab("more", "Guide")}{tab("chat", "Chat")}</nav>'
            + pane('now', ''.join(decide) or nothing, True)
            + pane('test', test_cards or '<div class="empty">✓ Nothing to test right now.<div class="ts">New builds to try appear here by themselves.</div></div>')
            + pane('ideas', ideas) + pane('more', more_pane(heads)) + pane('chat', chat_tab_html())
            + f'<div class="foot">{claude_bar()}{codex_reader()}</div></div>')


def guide_mark(n):
    return f'<i class="gm">{n}</i>'


def legend(items):
    return '<div class="gl">' + ''.join(f'<div><i class="gm in">{n}</i><span><b>{E(a)}</b> {E(b)}</span></div>' for n, (a, b) in enumerate(items, 1)) + '</div>'


def chat_tab_html():
    st = chat_settings()
    slide = lambda name, label, low, high, unit: (f'<label><span>{label}<b>{st[name]}{unit}</b></span><input type="range" name="{name}" min="{low}" max="{high}" value="{st[name]}"'
                                                  f' oninput="this.previousElementSibling.lastElementChild.textContent=this.value+&#39;{unit}&#39;" onchange="setChat(this)"></label>')
    pick = lambda name, label: f'<label><span>{label}</span><select name="{name}" onchange="setChat(this)">' + ''.join(f'<option value="{v}"{" selected" if st[name] == v else ""}>{t}</option>' for v, t in (('suggest', 'Claude suggests it'), ('auto', 'Automatic, at a good moment'))) + '</select></label>'
    return ('<div id="chatnow" class="chatnow"></div><div class="gset"><label class="on"><span>Show chat size and the New chat button</span><input type="checkbox" name="on" onchange="setChat(this)"' + (' checked' if st['on'] else '') + '></label>'
            + pick('compact_mode', 'Compacting') + slide('compact_at', 'Compact at', 30, 70, '%')
            + pick('new_mode', 'New chat') + slide('switch_at', 'New chat at', 50, 90, '%') + slide('compacts', 'Or after this many compacts', 2, 5, '')
            + '<label><span>Chat size</span><select name="window" onchange="setChat(this)">' + ''.join(f'<option value="{v}"{" selected" if st["window"] == v else ""}>{t}</option>' for v, t in ((1000000, 'Large (1M)'), (200000, 'Normal (200k)'))) + '</select></label>'
            + '<label><span>Style</span><select id="stylepick" onchange="setStyle(this.value);this.blur()">' + ''.join(f'<option value="{v}">{t}</option>' for v, t in (('auto', 'Same as my computer'), ('dark', 'Dark'), ('light', 'Light'), ('claude', 'Claude'))) + '</select></label></div>'
            + legend([('Bar under Claude', 'shows up when it is time to compact or start a new chat. The two marks are your settings.'), ('Copy /compact', 'with Claude suggests it, this copies the command for you to paste in your chat.'),
                      ('Automatic compacting', 'Claude compacts by itself from your percentage, and waits while it is building or checking. It starts with your next new chat.'),
                      ('Automatic New chat', 'when Claude finishes its work and the chat is full enough, it writes the hand-off and asks for a fresh chat once. You may need to click once to start it.'),
                      ('New chat', 'your chat writes a hand-off, opens a fresh chat, renames and pins it, and that chat shows the next step as a card. The app may ask you to click once to start it.')]))


KNOWN_COMMANDS = (('Make a ticket for ...', 'a new card'), ('Ask me that as a card', 'a choice with buttons'), ('Add this idea: ...', 'goes to Ideas'),
                  ('That works', 'finishes the test'), ('Partly: ...', 'keeps it open with your note'), ('Doesn\u2019t work: ...', 'back to your AI'),
                  ('Park that', 'put aside, can come back'), ('Mark it done', 'moves it to Done'), ('What\u2019s on the board?', 'a short summary'), ('Where did we leave off?', 'picks up from the notes'), ('New chat', 'hand-off, then a fresh chat'))


def guide_html():
    gm = guide_mark
    choice = ('<div class="tc dk" data-demo="1"><div class="tc-h"><span class="dk-area">Website' + gm(1) + '</span><b>dark mode</b></div>'
              '<div class="tc-q dk-q">Which dark colour should the site use?' + gm(2) + '</div>'
              '<a class="dk-go"><small>★ My pick</small>Soft grey' + gm(3) + '</a><div class="dk-alts"><a class="dk-alt">True black' + gm(4) + '</a><a class="dk-alt dk-else">✎ Something else' + gm(5) + '</a></div>'
              '<div class="gfold">Details ▾' + gm(6) + '</div></div>')
    choice += legend([('Label', 'which part of your project this is about.'), ('Question', 'the choice your AI needs from you.'), ('Big button', 'what your AI recommends. One press, then confirm once.'),
                      ('Small buttons', 'the other choices.'), ('Something else', 'none of these? Say it in your own words. You can paste a picture too.'), ('Details', 'one click shows what each choice costs and why your AI picked one.')])
    test = ('<div class="tc hl-claude" data-demo="1"><div class="hl-tag">✓ Claude done · your turn' + gm(1) + '</div><div class="tc-h"><b>Website: signup page</b></div><div class="ts tc-g">Sign up with a test address and check the welcome mail' + gm(2) + '</div>'
            '<div class="tc-row n3"><a class="b2">Works' + gm(3) + '</a><a class="b2">Partly' + gm(4) + '</a><a class="b2">Doesn\u2019t work' + gm(5) + '</a></div><div class="tc-row"><a class="b2">Feedback provided' + gm(6) + '</a></div>'
            '<div class="gfold right">More ▾' + gm(7) + '</div></div>')
    test += legend([('Outline and tag', 'this is what Claude just finished. The board opens this tab by itself, once.'), ('What to check', 'in plain words, the thing to try.'), ('Works', 'it is done.'),
                    ('Partly', 'tell what works and what does not; the rest gets fixed.'), ('Doesn\u2019t work', 'tell what you saw; a fix comes next.'), ('Feedback provided', 'you already told your AI in chat, so the card can close.'), ('More', 'Ask about this: a question without giving a result.')])
    yes = ('<div class="tc small yk" data-demo="1"><div class="tc-h"><span class="dk-area">Shop</span><b>gift cards</b></div><div class="ts tc-g">Sell gift cards in the shop</div>'
           '<div class="tc-row"><a class="b2">Build it' + gm(1) + '</a><a class="b2">Not now' + gm(2) + '</a></div><div class="gfold right">More ▾' + gm(3) + '</div></div>')
    yes += legend([('Build it', 'yes, go ahead. Nothing is built until you say so.'), ('Not now', 'put aside. Say so in chat to bring it back.'), ('More', 'Ask about this: a question first. Already works: closes it without building. Drop it: you do not want it.')])
    ask = ('<div class="tc qk" data-demo="1"><div class="tc-h"><span class="dk-area">Claude asked</span></div><div class="tc-q dk-q">Do you have a photo of the shop?</div>'
           '<div class="tc-row"><a class="b2">Answered' + gm(1) + '</a><a class="b2">Reply' + gm(2) + '</a></div></div>')
    ask += legend([('Answered', 'you already answered in chat; this closes the card.'), ('Reply', 'write your answer here instead.')])
    screen = legend([('Numbers on the tabs', 'how many cards wait for you there.'), ('Colour and headings', 'purple is a choice, blue is Build it?, amber is a question.'), ('Then (2)', 'more cards on the same subject. Open it when the first one is settled.'),
                     ('This chat', 'the line on top: this chat\u2019s latest prompt, or No new information yet.'),
                     ('Bottom bar', 'Claude\u2019s steps: green is done, yellow is the step now. Your test means it waits for you. The Codex line only reads what Codex did last in this folder.'), ('It updates by itself', 'no need to reload; your tab and scroll stay where they are.'),
                     ('Pressed a button?', 'your AI sees it the next time it replies, so carry on in chat.')])
    ideas = legend([('Ideas', 'everything you mentioned that is not built yet.'), ('Make prompt', 'gathers your ideas into one message.'), ('Copy prompt', 'paste it in your chat and your AI turns the ideas into cards for you to approve.')])
    said = ''.join(f'<div class="row"><span>{E(a)}</span><span class="ts">{E(b)}</span></div>' for a, b in KNOWN_COMMANDS)
    fold = lambda t, inner, shown=False: f'<details class="bsec guide"{" open" if shown else ""}><summary>{t}</summary>{inner}</details>'
    return (fold('What you can say in your chat', said, True) + fold('A choice card', choice) + fold('A test card', test) + fold('A Build it? card', yes) + fold('A question card', ask)
            + fold('The rest of the screen', screen) + fold('The Ideas tab', ideas))


def more_pane(heads):
    sec = lambda title, inner: f'<details class="bsec"><summary>{title}</summary>{inner}</details>'
    notes = [l for l in read(P('NOTES.md')).splitlines() if l.strip()]
    left = f'<div class="g" style="font-size:13px;line-height:1.45">{E(notes[0])}</div>' if notes else '<div class="ts">No hand-over yet.</div>'
    done = [(when_ts(ticket_state(h)['when']), h) for h in heads if ticket_state(h)['state'] in ('done', 'works')]
    done.sort(key=lambda x: x[0], reverse=True)
    rows = ''.join(f'<div class="row"><span>{E(short_name(h))}</span><span class="ts">{E(rel_age(t))}</span></div>' for t, h in done[:8]) or '<div class="ts">Nothing finished yet.</div>'
    return guide_html() + sec('Where we left off', left) + sec(f'Done ({len(done)})', rows)


def claude_active():
    try:
        return time.time() - os.path.getmtime(P('CLAUDE-PROGRESS.json')) < 300
    except OSError:
        return False


def status_dot(name, on):
    return f'<i class="dot{" on" if on else ""}"></i><strong>{name}</strong> · '


def step_bar(step, text, live, head):
    label_style = 'font-size:11px;line-height:13px;white-space:normal;text-align:center;padding:3px'
    segs = ''.join(f'<i{" class=now" if live and k == step else ""} style="min-height:24px;height:auto;{"background:#45a466" if k <= step else ""}"><em style="{label_style}">{E(name)}</em></i>' for k, name in enumerate(CLAUDE_STEPS))
    return f'<div class="fbar"><span style="white-space:normal;font-size:13px;line-height:19px">{head}{E(text)}</span><b>{segs}</b></div>'


def chat_bar(cid):
    """Claude's line for one chat, or '' when this project's chats do not keep their own yet."""
    folder = P('chat-steps')
    if not os.path.isdir(folder) or not re.fullmatch(r'[0-9a-zA-Z-]{8,64}', cid or ''):
        return ''
    p = os.path.join(folder, cid + '.json')
    try:
        data = json.load(open(p, encoding='utf-8'))
        return claude_bar(data, data.get('live', True) and time.time() - os.path.getmtime(p) < (900 if 'live' in data else 300))
    except (OSError, ValueError):
        return claude_bar({}, False)   # this chat has not said anything yet: never show another chat's step


def claude_bar(progress=None, on=None):
    progress = json_file('CLAUDE-PROGRESS.json') if progress is None else progress
    on = (claude_active() and progress.get('live', True)) if on is None else on
    step = max(0, min(int(progress.get('step', 0)), len(CLAUDE_STEPS) - 1))
    words = progress.get('task') if progress.get('task') != 'Reading your message' else ''   # that line was left by older versions
    if (on and (words or 'step' in progress)) or (words and step == len(CLAUDE_STEPS) - 1):
        return step_bar(step, words or CLAUDE_STEPS[step], on, status_dot('Claude', on))
    return f'<div class="fbar"><span>{status_dot("Claude", on)}{"working" if on else "idle"}</span></div>'


_CX = {'cwd': {}, 't': 0, 'html': ''}


def _cx_folder(path):
    if path not in _CX['cwd']:   # the first line of a log never changes, so each log is opened for this once
        with open(path, encoding='utf-8', errors='replace') as f:
            _CX['cwd'][path] = json.loads(f.readline(2000000))['payload']['cwd']
    return _CX['cwd'][path]


def _cx_last(path):
    with open(path, 'rb') as f:
        size = f.seek(0, 2)
        f.seek(max(0, size - 300000))
        lines = f.read().decode('utf-8', 'replace').split(chr(10))
    working, said = None, ''
    for l in reversed(lines[1:] if size > 300000 else lines):
        if working is not None and said:
            break
        if 'task_' not in l and '"assistant"' not in l:
            continue
        try:
            o = json.loads(l)
        except ValueError:
            continue
        pl = o.get('payload') or {}
        if o.get('type') == 'event_msg' and pl.get('type') in ('task_started', 'task_complete'):
            working = pl['type'] == 'task_started' if working is None else working
            said = said or pl.get('last_agent_message') or ''
        elif o.get('type') == 'response_item' and pl.get('type') == 'message' and pl.get('role') == 'assistant' and not said:
            said = ' '.join(x.get('text', '') for x in pl.get('content') or [] if isinstance(x, dict))
    return bool(working), said


def _cx_line():
    same = lambda p: os.path.normcase(os.path.normpath(p))
    logs = glob.glob(os.path.join(os.path.expanduser('~'), '.codex', 'sessions', '**', 'rollout-*.jsonl'), recursive=True)
    line = lambda on, text: f'<div class="fbar"><span>{status_dot("Codex", on)}{E(text)}</span></div>'
    for mtime, p in sorted(((os.path.getmtime(p), p) for p in logs), reverse=True):
        if time.time() - mtime > 7 * 86400:
            break
        try:
            if same(_cx_folder(p)) != same(ROOT):
                continue
        except (OSError, ValueError, KeyError, TypeError):
            continue
        working, said = _cx_last(p)
        working = working and time.time() - mtime < 600
        said = re.split(r'(?<=[.!?:])\s', re.sub(r'[*`#]+', '', said).strip(), maxsplit=1)[0].splitlines()[0] if said.strip() else ''
        said = said[:110].rstrip() + '…' if len(said) > 110 else said
        age = time.time() - mtime
        when = 'working' if working else 'just now' if age < 60 else rel_age(mtime) + ' ago'
        return line(working, when + (': ' + said if said else ''))
    return line(False, 'no recent work') if os.path.isdir(os.path.join(os.path.expanduser('~'), '.codex')) else ''


def codex_reader():
    if time.time() - _CX['t'] > 20:   # only reads logs of this folder, writes nothing, and stays quiet if their format changes
        _CX['t'] = time.time()
        try:
            _CX['html'] = _cx_line()
        except Exception:
            _CX['html'] = ''
    return _CX['html']


def sync_compact_setting(st):
    """Automatic compacting uses Claude's own compact, told to start at the person's percentage. A value they set themselves stays."""
    p = os.path.join(ROOT, '.claude', 'settings.json')
    name = 'CLAUDE_AUTOCOMPACT_PCT_OVERRIDE'
    try:
        data = json.load(open(p, encoding='utf-8-sig')) if os.path.exists(p) else {}
        env = data.setdefault('env', {})
        if st['compact_mode'] == 'auto' and (st['pct_set'] or name not in env):
            env[name] = str(st['compact_at'])
            st['pct_set'] = True
        elif st['compact_mode'] != 'auto' and st['pct_set']:
            env.pop(name, None)
            st['pct_set'] = False
        if not env:
            data.pop('env')
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except (OSError, ValueError):
        pass


def chat_settings():
    d = json_file('CHAT-SETTINGS.json')
    num = lambda k, low, high, dflt: max(low, min(high, int(d.get(k, dflt)))) if str(d.get(k, dflt)).isdigit() else dflt
    mode = lambda k: 'auto' if d.get(k) == 'auto' else 'suggest'
    return {'on': d.get('on', True) is True, 'compact_mode': mode('compact_mode'), 'new_mode': mode('new_mode'), 'pct_set': d.get('pct_set') is True, 'compact_at': num('compact_at', 30, 70, 50), 'switch_at': num('switch_at', 50, 90, 70), 'compacts': num('compacts', 2, 5, 2),
            'window': 200000 if str(d.get('window')) == '200000' else 1000000}


def prompt_of(o):
    """The person's words in one line of a chat log, without notes the app put around them."""
    if o.get('isMeta'):
        return ''
    if o.get('type') == 'attachment':
        a = o.get('attachment') or {}
        t = a.get('prompt') if a.get('type') == 'queued_command' else ''
    else:
        t = (o.get('message') or {}).get('content') if o.get('type') == 'user' else ''
    if isinstance(t, list):
        t = ' '.join(x.get('text', '') for x in t if isinstance(x, dict) and x.get('type') == 'text')
    if not isinstance(t, str):
        return ''
    while True:
        s = re.sub(r'^\s*<([\w-]+)[^>]*>.*?</\1>\s*', '', t, count=1, flags=re.S)
        if s == t:
            break
        t = s
    return '' if t.lstrip().startswith('<') else ' '.join(t.split())[:110]


_PROMPTS, _PROMPT_LOCK = {}, threading.Lock()


def latest_prompt(path):
    """The chat's newest prompt. Every open page asks every few seconds, so the answer is kept until the log changes,
    and only one page reads a log at a time (a long chat log is many MB)."""
    try:
        st = os.stat(path)
    except (OSError, TypeError, ValueError):
        return ''
    key = (st.st_size, st.st_mtime)
    with _PROMPT_LOCK:
        if _PROMPTS.get(path, (None,))[0] == key:
            return _PROMPTS[path][1]
        text = read_prompt(path)
        _PROMPTS[path] = (key, text)
        return text


def read_prompt(path):
    for size in (512000, 16000000):
        try:
            with open(path, 'rb') as f:
                whole = f.seek(0, 2) <= size
                f.seek(max(0, f.tell() - size))
                lines = f.read().decode('utf-8', 'replace').splitlines()
        except (OSError, TypeError, ValueError):
            return ''
        for l in reversed(lines):
            if 'user"' not in l and 'queued_command' not in l:
                continue
            try:
                t = prompt_of(json.loads(l))
            except (ValueError, AttributeError):
                continue
            if t:
                return t
        if whole:
            break
    return ''


def chat_info(cid):
    chats = json_file('CHATS.json')
    if not cid and chats:
        cid = max(chats, key=lambda k: chats[k].get('start', 0))
    c = chats.get(cid)
    if not c:
        return {'known': False}
    last = latest_prompt(c.get('transcript', ''))
    newest = 0
    for n in (('tickets', 'TICKETS.md'), ('DECISIONS.md',), ('IDEAS.md',), ('OPEN-QUESTIONS.md',), ('CLAUDE-PROGRESS.json',)):
        try:
            newest = max(newest, os.path.getmtime(P(*n)))
        except OSError:
            pass
    st = chat_settings()
    window = st['window']
    used = 0
    try:
        with open(c.get('transcript', ''), 'rb') as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 400000))
            for l in f.read().decode('utf-8', 'replace').splitlines():
                if '"usage"' not in l:
                    continue
                try:
                    o = json.loads(l)
                except ValueError:
                    continue
                u = (o.get('message') or {}).get('usage') or {}
                if u and not o.get('isSidechain'):
                    used = int(u.get('input_tokens') or 0) + int(u.get('cache_read_input_tokens') or 0) + int(u.get('cache_creation_input_tokens') or 0)
    except (OSError, TypeError):
        pass
    if used > window:
        window = 1000000
    sw = json_file('SWITCH-STATE.json')
    step = sw.get('step') if time.time() - sw.get('t', 0) < (120 if sw.get('step') == 4 else 900) else None
    return {'known': True, 'last': last, 'fresh': newest <= c.get('start', 0), 'updated': time.strftime('%H:%M', time.localtime(newest)) if newest else '',
            'on': st['on'], 'compact_mode': st['compact_mode'], 'compact_at': st['compact_at'], 'switch_at': st['switch_at'], 'max': st['compacts'], 'pct': round(100 * used / window) if used else None,
            'compacts': int(c.get('compacts', 0)), 'pending': os.path.exists(P('SWITCH-REQUEST.json')), 'step': step, 'bar': chat_bar(cid)}


def style_js():
    try:
        d = json.load(open(os.path.expanduser('~/.scrumdash-style.json'), encoding='utf-8'))
        start, at = (d['style'], int(d['at'])) if d['style'] in ('dark', 'light', 'claude', 'auto') else ('auto', 0)
    except (OSError, ValueError, KeyError, TypeError):
        start, at = 'auto', 0
    return ("(function(){const K='scrumdash-style',h=document.documentElement,m=matchMedia('(prefers-color-scheme:dark)');let cur='" + start + "';"
            "try{const o='claude'+'dex-style';if(!localStorage.getItem(K)&&localStorage.getItem(o)){localStorage.setItem(K,localStorage.getItem(o));localStorage.setItem(K+'-at',localStorage.getItem(o+'-at')||'0')}}catch(e){}"
            "try{const s=localStorage.getItem(K);if(s&&+(localStorage.getItem(K+'-at')||0)>=" + str(at) + ")cur=s}catch(e){}"
            "function show(){h.dataset.style=cur==='auto'?(m.matches?'dark':'light'):cur}"
            "window.getStyle=()=>cur;window.setStyle=v=>{cur=v;try{localStorage.setItem(K,v);localStorage.setItem(K+'-at',Math.floor(Date.now()/1000))}catch(e){}show()};"
            "show();m.addEventListener('change',show)})()")


PASTE_JS = r'''(()=>{
// Paste a copied image into any answer box, or pick one with the 📎 link; sent in small GET pieces.
const K='__K__';
function setup(t){if(t.dataset.pz||!t.form)return;t.dataset.pz=1;
 const b=document.createElement('label');b.textContent='📎 Add image (or paste one)';b.style.cssText='font-size:12px;color:var(--lnk);cursor:pointer;display:block;margin:2px 0';
 const i=document.createElement('input');i.type='file';i.accept='image/*';i.style.display='none';b.appendChild(i);
 i.addEventListener('change',()=>{if(i.files[0])send(t,i.files[0])});t.insertAdjacentElement('afterend',b);}
function scan(){document.querySelectorAll('textarea[name=x]').forEach(setup)}
async function send(t,file){const f=t.form;let s=f.querySelector('.pz-s');
 if(!s){s=document.createElement('div');s.className='pz-s';s.style.cssText='font-size:12px;color:var(--mute)';t.insertAdjacentElement('afterend',s)}
 s.textContent='Adding image…';const btn=f.querySelector('button');if(btn)btn.disabled=true;
 try{const im=await createImageBitmap(file);const sc=Math.min(1,1600/Math.max(im.width,im.height));
  const c=document.createElement('canvas');c.width=Math.round(im.width*sc);c.height=Math.round(im.height*sc);
  c.getContext('2d').drawImage(im,0,0,c.width,c.height);const d=c.toDataURL('image/jpeg',.85).split(',')[1];
  const id=[...crypto.getRandomValues(new Uint8Array(6))].map(x=>x.toString(16).padStart(2,'0')).join('');const N=6000,n=Math.ceil(d.length/N);
  for(let k=0;k<n;k++){const r=await fetch('/img?k='+K+'&id='+id+'&i='+k+'&n='+n+'&d='+encodeURIComponent(d.slice(k*N,(k+1)*N)),{credentials:'same-origin',cache:'no-store'});
   if(!r.ok)throw 0;s.textContent='Adding image… '+Math.round((k+1)*100/n)+'%'}
  let h=f.querySelector('input[name=img]');if(!h){h=document.createElement('input');h.type='hidden';h.name='img';f.appendChild(h)}h.value=id;
  s.textContent='';const p=document.createElement('img');p.src='data:image/jpeg;base64,'+d;p.style.cssText='max-width:120px;max-height:80px;border-radius:4px;vertical-align:middle;margin-right:6px';
  const x=document.createElement('a');x.textContent='remove';x.href='#';x.onclick=e=>{e.preventDefault();h.remove();s.remove();t.required=t.dataset.req==='1'};
  s.append(p,'Image attached · ',x);t.dataset.req=t.required?'1':'';t.required=false;
 }catch(e){s.textContent='Image could not be added'}finally{if(btn)btn.disabled=false}}
document.addEventListener('paste',e=>{const t=e.target.closest&&e.target.closest('textarea[name=x]');if(!t)return;
 const it=[...((e.clipboardData||{}).items||[])].find(x=>x.type.startsWith('image/'));if(!it)return;e.preventDefault();send(t,it.getAsFile())});
scan();new MutationObserver(scan).observe(document.documentElement,{childList:true,subtree:true});
})();
'''

LOGO_INNER = ('<defs><linearGradient id="cdx" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#8ab4f8"/><stop offset="1" stop-color="#1a73e8"/></linearGradient></defs><path d="M12 1.8 20.8 6.9v10.2L12 22.2 3.2 17.1V6.9Z" fill="none" stroke="url(#cdx)" stroke-width="2" stroke-linejoin="round"/><g stroke="url(#cdx)" stroke-width="2.2" stroke-linecap="round"><path d="M12 7v10M7 12h10M8.5 8.5l7 7M15.5 8.5l-7 7"/></g><circle cx="12" cy="12" r="1.6" fill="#e8f0fe"/>')
LOGO = '<svg viewBox="0 0 24 24" width="22" height="22" role="img" aria-label="Scrumdash">' + LOGO_INNER + '</svg>'
# Saved per browser. Decides before the page paints, so a saved style never flashes the wrong colours.
STYLE_HEAD = ('<style>:root[data-style=light]{color-scheme:light;--bg:#f7f7f8;--card:#fff;--ink:#202124;--mute:#5f6368;--line:#e3e3e6;--lnk:#1a73e8}'
              ':root[data-style=dark]{color-scheme:dark;--bg:#17181a;--card:#222326;--ink:#e8eaed;--mute:#9aa0a6;--line:#35363a;--lnk:#8ab4f8}'
              ':root[data-style=claude]{color-scheme:light;--bg:#f5f4ed;--card:#fffefb;--ink:#141413;--mute:#6b6a63;--line:#e3e0d5;--lnk:#b3502f}</style>'
              '<link rel="icon" href="data:image/svg+xml,' + urllib.parse.quote('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">' + LOGO_INNER + '</svg>') + '">'
              '<script src="/style.js"></script>')
SMALL_PAGE = ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">\n'
              '<title>Scrumdash</title>' + STYLE_HEAD + '<style>body{margin:0;background:var(--bg);color:var(--ink);font:15px system-ui,sans-serif;display:grid;place-items:center;min-height:100vh}\n'
              'form{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:22px;display:grid;gap:10px;width:min(260px,calc(100vw - 80px))} *{box-sizing:border-box}\n'
              'input,button{font:inherit;padding:8px;border-radius:6px;border:1px solid var(--line);background:var(--bg);color:inherit}\n'
              'button{background:#1a73e8;color:#fff;border:0;cursor:pointer}</style></head><body>\n')
NOTE_BOX = ('<textarea name="x" rows="7" maxlength="4000" required autofocus style="font:inherit;padding:8px;border-radius:6px;border:1px solid var(--line);background:var(--bg);color:inherit"></textarea>'
            '<button>Send to Claude</button><a href="/" style="text-align:center;padding:6px;color:var(--mute)">Cancel</a></form><script src="/paste.js"></script>')
BIG_LINK = 'text-align:center;padding:8px;border-radius:6px;background:#1a73e8;color:#fff;text-decoration:none'

CSS = r''':root{--bg:#f7f7f8;--card:#fff;--ink:#202124;--mute:#5f6368;--line:#e3e3e6}
:root[data-style=dark]{--bg:#17181a;--card:#222326;--ink:#e8eaed;--mute:#9aa0a6;--line:#35363a}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,Segoe UI,sans-serif}
.okrow{padding:6px 0}
h2{margin:0 0 8px;font-size:15px}
.ts{color:var(--mute);font-size:12px;margin-right:8px}
details{padding:6px 0;border-bottom:1px solid var(--line)}
summary{cursor:pointer}
details[open],details[open] *{-webkit-line-clamp:unset!important;line-clamp:unset!important;max-height:none!important;overflow-wrap:anywhere}
details[open]{overflow:visible!important;display:block!important;height:auto!important;white-space:normal}
.g{padding:5px 0;font-size:15px}
.okrow{padding:10px 0;border-bottom:1px solid var(--line)}
li{margin:2px 0}
.main{max-width:760px;margin:auto;padding:16px;display:grid;gap:12px}
.sec{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.sec h2{font-size:17px;margin:0 0 8px}
.m2{max-width:640px;padding:12px;gap:10px}
.m2 .sec{border-radius:10px;padding:12px 14px}
.m2 .sec>h2{font-size:12px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--mute);margin:0 0 6px}
.m2 .row{display:flex;justify-content:space-between;gap:12px;padding:7px 0;border-top:1px solid var(--line);font-size:14px}
.m2 .row:first-of-type{border-top:0}
.m2 .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px;background:var(--mute)}
.m2 .dot.on{background:#1e8e3e}
.m2 .okrow{padding:10px 0;border:0;border-top:1px solid var(--line)}
.m2 .ts{font-size:13px}
.m2 .b2{display:inline-block;margin:8px 6px 0 0;padding:7px 13px;border-radius:8px;border:1.5px solid color-mix(in srgb,var(--ink) 40%,transparent);background:color-mix(in srgb,var(--ink) 5%,transparent);color:var(--ink);font:600 14px system-ui,sans-serif;text-decoration:none;cursor:pointer;line-height:1.2}
.m2 .b2:hover{background:color-mix(in srgb,var(--ink) 12%,transparent)}
.m2 .b2:active{background:color-mix(in srgb,var(--ink) 20%,transparent)}
.m2 select{font:14px system-ui,sans-serif!important;padding:6px!important}
.m2 details{border:0;padding:0}
.m2 details>summary{font-size:13px;font-weight:600;color:var(--mute);padding:8px 0 4px}
.main{grid-template-columns:minmax(0,1fr)}
.sec{min-width:0;overflow-wrap:anywhere}
html,body{overflow-x:hidden}
@media (max-width:620px){.main.m2{padding:6px;gap:8px}.m2 .sec{padding:10px 11px}.m2 .okrow{padding:8px 0}.m2 .okrow .ts{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.m2 .okrow .b2,.m2 .okrow form,.m2 .okrow button{display:inline-block!important;float:none!important;padding:5px 10px;font-size:13px;margin:6px 6px 0 0;width:auto}.m2 .row{padding:5px 0;font-size:13px}}
.m2 .okrow .b2{padding:4px 10px;font-size:13px;font-weight:500;margin:6px 6px 0 0}
.m2 .grp{font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--mute);margin:12px 0 0;padding-top:6px}
.m2 .grp:first-child{margin-top:0}
.m2 details.foldsec>summary{list-style:none;cursor:pointer;padding:0}
.m2 details.foldsec>summary::-webkit-details-marker{display:none}
.m2 details.foldsec>summary>h2{display:inline-block;font-size:12px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--mute);margin:0}
.m2 details.foldsec>summary>h2::before{content:'▸ '}
.m2 details.foldsec[open]>summary>h2::before{content:'▾ '}
.m2 details.foldsec[open]>summary{margin-bottom:6px}
@media (max-width:380px){.m2 .okrow .b2{padding:5px 9px;font-size:12.5px;margin:5px 5px 0 0}}
.main.board .foot{row-gap:2px}
.main.board .tc .tc-g{display:block;overflow:visible;overflow-wrap:anywhere}
.main.board .tabs .tab{white-space:nowrap;padding-left:1px;padding-right:1px;font-size:12.5px;min-width:0}
.main.board .tabs .tab i{margin-left:3px}
.main.board .foot .fbar{flex:1 1 100%;background:#3c4043;border-radius:8px;overflow:hidden;margin-bottom:3px;padding:2px 4px 4px}
.main.board .foot .fbar b{display:flex;gap:3px;margin-top:2px}
.main.board .foot .fbar b i{flex:1;min-width:0;height:15px;border-radius:3px;outline:1px solid #5f6368;display:flex;align-items:center;justify-content:center}
.main.board .foot .fbar b em{font-style:normal;font-size:9px;line-height:15px;color:#fff;text-shadow:0 0 2px #000;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding:0 2px}
.main.board .foot .fbar span .dot{display:inline-block;margin-right:5px;vertical-align:1px}
.main.board .foot .fbar span strong{font-weight:600}
.main.board .foot .fbar span{display:block;padding:0 4px;line-height:17px;font-size:11.5px;color:#fff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.main.board .foot .fbar span{white-space:normal;font-size:13px;line-height:19px}
.main.board .foot .fbar b i{height:auto;min-height:24px}
.main.board .foot .fbar b em{white-space:normal;font-size:11px;line-height:13px;text-align:center;padding:3px}
.main.board .tc .b2,.main.board .foot .b2{display:block;width:100%;box-sizing:border-box;margin:0;padding:7px 6px;text-align:center;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:13px;line-height:1.25}
.main.board .tc-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(96px,1fr));gap:6px;margin-top:8px}
.main.board .tc-row.n3{grid-template-columns:1fr 1fr 1fr}
.main.board .tc-row form,.main.board .tc-row details{grid-column:1/-1}
.main.board details.tc-more{border:0;padding:0;margin:4px 0 0}
.main.board details.tc-more>summary{list-style:none;text-align:right;font-size:12px;color:var(--mute);cursor:pointer;padding:2px 0}
.main.board details.tc-more>summary::-webkit-details-marker{display:none}
.main.board details.tc-more>summary::after{content:' ▾'}
.main.board details.tc-more[open]>summary::after{content:' ▴'}
.main.board details.tc-more>.tc-row{margin-top:2px}
.main.board .tc{padding:10px 12px;margin:0 0 8px;border-radius:12px}
.main.board .tc-h{font-size:14px;gap:1px 6px}
.main.board .tc-h .ts{font-size:11.5px}
.main.board .tc-g{font-size:13px;margin-top:1px}
.main.board .tc-q{font-size:13.5px;margin-top:9px;padding-top:7px}
.main.board .pane{padding:2px 8px 10px}
.main.board .tabs{padding:6px 8px 4px}
.main.board .tab{padding:7px 2px;font-size:12.5px}
.main.board .tab i{margin-left:4px;min-width:16px;line-height:17px;font-size:10.5px}
.main.board .tc.small{padding:8px 11px}
.main.board .tc.small .tc-h{font-size:13.5px}
.main.board .foot{flex-wrap:wrap;gap:1px 8px;padding:4px 8px}
.main.board .foot .b2{padding:6px 6px;font-size:12.5px}
.main.board details.then{border:0;padding:0;margin:8px 0 0;border-top:1px solid var(--line)}
.main.board details.then>summary{list-style:none;cursor:pointer;font-size:12px;color:var(--mute);padding:6px 0 0}
.main.board details.then>summary::-webkit-details-marker{display:none}
.main.board details.then>summary::before{content:'▸ '}
.main.board details.then[open]>summary::before{content:'▾ '}
.main.board details.then .tc-row:first-child,.main.board details.then .tc-q:first-child{border-top:0;padding-top:0;margin-top:6px}
.main.board .tc-g{-webkit-line-clamp:unset}
.tc-list{margin:4px 0 2px 0;padding-left:18px;font-size:13px;line-height:1.45}
.tc-list li{margin:1px 0}
@media (min-width:461px){.main.board .foot .b2{width:auto;padding:6px 12px}}
html,body{height:100%;overflow:hidden}
.main.board{display:flex;flex-direction:column;height:100dvh;max-width:620px;margin:auto;padding:0;gap:0;background:var(--bg)}
.tabs{flex:none;display:flex;gap:4px;padding:8px 10px 6px;background:var(--bg);position:relative;z-index:2}
.tab{flex:1;border:0;border-radius:10px;padding:8px 4px;font:600 13px system-ui,sans-serif;color:var(--mute);background:transparent;cursor:pointer;transition:background .15s,color .15s;-webkit-tap-highlight-color:transparent}
.tab i{font-style:normal;display:inline-block;min-width:18px;padding:0 5px;margin-left:5px;border-radius:9px;background:color-mix(in srgb,var(--ink) 10%,transparent);font-size:11px;line-height:18px;transition:transform .2s}
.tab.on{background:var(--card);color:var(--ink);box-shadow:0 1px 3px rgba(0,0,0,.12)}
.tab.on i{background:#1a73e8;color:#fff}
.tab i.bump{animation:bump .4s}
@keyframes bump{30%{transform:scale(1.35)}}
.pane{flex:1 1 auto;min-height:0;overflow:auto;padding:4px 10px 12px;overscroll-behavior:contain}
.pane[hidden]{display:none}
.pane.in{animation:in .18s ease-out}
@keyframes in{from{opacity:0;transform:translateY(4px)}}
.tc{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:11px 13px;margin:0 0 8px;box-shadow:0 1px 2px rgba(0,0,0,.05);transition:box-shadow .2s}
.tc-h{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 8px;font-size:15px;line-height:1.3}
.tc-h .ts{margin:0;font-size:12px}
.tc-g{margin-top:2px;overflow-wrap:anywhere}
.tc-row{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;align-items:center}
.tc-row .b2,.tc-row form{margin:0!important}
.tc-q{font-weight:600;margin-top:10px;padding-top:8px;border-top:1px solid var(--line)}
.tc.small{padding:9px 12px;border-radius:12px}
.tc.small .tc-h{font-size:14px}
.tc .okrow{border:0;padding:0;margin:0;background:transparent;border-radius:0}
.tc .b2{margin:0 6px 0 0}
details.bsec{border:0;padding:0;margin:0 0 6px}
details.bsec>summary{font-size:12px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--mute);padding:8px 0 6px;list-style:none;cursor:pointer}
details.bsec>summary::-webkit-details-marker{display:none}
details.bsec>summary::before{content:'▸ '}
details.bsec[open]>summary::before{content:'▾ '}
details.bsec .row,.pane .row{display:flex;justify-content:space-between;gap:10px;padding:7px 0;border-top:1px solid var(--line);font-size:14px}
details.bsec .row:first-of-type{border-top:0}
details.bsec .g{padding:4px 0;font-size:14px}
.pane details.sec.foldsec{background:transparent;border:0;padding:0;margin-bottom:8px}
.pane details.sec.foldsec>summary>h2{font-size:12px}
.pane .okrow{border-top:1px solid var(--line);padding:8px 0}
.empty{text-align:center;padding:36px 10px;color:var(--ink);font-size:16px;font-weight:600}
.empty .ts{margin-top:6px;font-weight:400}
.foot{flex:none;display:flex;align-items:center;justify-content:space-between;gap:8px;padding:7px 10px;border-top:1px solid var(--line);background:var(--card);font-size:12px;color:var(--mute)}
.foot .b2{margin:0;padding:5px 10px;font-size:12.5px}
.foot .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--mute);margin-right:6px;vertical-align:0}
.foot .dot.on{background:#34a853;animation:pulse 1.6s infinite}
@keyframes pulse{50%{opacity:.35}}
.main.board .foot .fbar b i.now{animation:pulse 1.6s infinite;background:#e0b020!important}
@media (max-width:380px){.tab{font-size:12px;padding:7px 2px}}
.tc{--dk:#7c4dff}
.main.board .tc.dk{--ac:var(--dk)}
.main.board .tc.yk{--ac:#2f7cf6}
.main.board .tc.qk{--ac:#d99a00}
:root[data-style=dark] .tc{--dk:#a08cff}
:root[data-style=dark] .main.board .tc.yk{--ac:#5b9bff}
:root[data-style=dark] .main.board .tc.qk{--ac:#f0b33a}
.main.board .tc:is(.dk,.yk,.qk){background:linear-gradient(160deg,color-mix(in srgb,var(--ac) 14%,var(--card)) 0%,var(--card) 62%);border-color:color-mix(in srgb,var(--ac) 32%,var(--line));box-shadow:0 2px 12px color-mix(in srgb,var(--ac) 16%,transparent);padding:11px 12px 10px}
.main.board .tc:is(.dk,.yk,.qk) .tc-h{flex-wrap:nowrap;align-items:center;gap:7px;font-size:12px;color:var(--mute);min-width:0}
.main.board .tc:is(.dk,.yk,.qk) .tc-h b{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.main.board .tc .dk-area{flex:none;font-size:10px;font-weight:700;letter-spacing:.06em;line-height:14px;text-transform:uppercase;color:var(--ac,var(--dk));background:color-mix(in srgb,var(--ac,var(--dk)) 15%,transparent);padding:2px 7px;border-radius:999px}
.main.board .tc.yk .tc-h{flex-wrap:wrap;row-gap:5px}
.main.board .tc.yk .tc-h b{flex-basis:100%;white-space:normal;font-size:15.5px;font-weight:700;line-height:1.3;color:var(--ink)}
.main.board .tc.yk>.tc-row{grid-template-columns:1fr 1fr;margin-top:9px}
.main.board .tc.yk>.tc-row>.b2:first-child{grid-column:1/-1;color:#fff;border:0;font:700 15px/1.25 system-ui,sans-serif;padding:10px 12px;border-radius:12px;background:linear-gradient(135deg,var(--ac),color-mix(in srgb,var(--ac) 55%,#7c4dff));box-shadow:0 3px 10px color-mix(in srgb,var(--ac) 35%,transparent)}
.main.board .tc.qk>.tc-row>.b2,.main.board .tc.yk>.tc-row>.b2:not(:first-child){font-size:13px;padding:6px 8px;border-radius:10px;color:color-mix(in srgb,var(--ac) 70%,var(--ink));background:color-mix(in srgb,var(--ac) 9%,var(--card));border:1px solid color-mix(in srgb,var(--ac) 26%,transparent)}
.main.board .tc .dk-q{border-top:0;margin:7px 0 10px;padding:0;font-size:16px;font-weight:700;line-height:1.3;color:var(--ink)}
.main.board details.then .dk-q{border-top:1px solid var(--line);padding-top:8px}
.main.board .tc .dk-go{display:block;text-align:center;text-decoration:none;color:#fff;font:700 15px/1.25 system-ui,sans-serif;padding:8px 12px 10px;border-radius:12px;background:linear-gradient(135deg,var(--dk),color-mix(in srgb,var(--dk) 50%,#3b6cf6));box-shadow:0 3px 10px color-mix(in srgb,var(--dk) 35%,transparent);transition:transform .12s,box-shadow .12s}
.main.board .tc .dk-go small{display:block;font-size:10px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;opacity:.85;margin-bottom:1px}
.main.board .tc .dk-go:hover{box-shadow:0 5px 16px color-mix(in srgb,var(--dk) 45%,transparent)}
.main.board .tc .dk-go:active{transform:scale(.98)}
.main.board .tc .dk-alts{display:grid;grid-template-columns:repeat(auto-fit,minmax(112px,1fr));gap:6px;margin-top:7px}
.main.board .tc .dk-alt{display:flex;align-items:center;justify-content:center;text-align:center;min-height:32px;box-sizing:border-box;padding:5px 8px;border-radius:10px;text-decoration:none;font:600 13px/1.2 system-ui,sans-serif;color:color-mix(in srgb,var(--dk) 70%,var(--ink));background:color-mix(in srgb,var(--dk) 9%,var(--card));border:1px solid color-mix(in srgb,var(--dk) 26%,transparent)}
.main.board .tc .dk-alt:hover{background:color-mix(in srgb,var(--dk) 17%,var(--card))}
.main.board .tc .dk-alt.dk-else{color:var(--mute);background:transparent;border:1px dashed var(--line);font-weight:500}
.main.board .tc details.dk-more{border:0;margin:7px 0 0;padding:0}
.main.board .tc details.dk-more>summary{list-style:none;display:inline-block;cursor:pointer;font-size:12px;font-weight:500;color:var(--mute);padding:2px 0}
.main.board .tc details.dk-more>summary::-webkit-details-marker{display:none}
.main.board .tc details.dk-more>summary::after{content:' ▾'}
.main.board .tc details.dk-more[open]>summary::after{content:' ▴'}
.main.board .tc details.dk-more p{margin:6px 0 0;font-size:12.5px;line-height:1.4}
.main.board .tc details.dk-more p b{color:color-mix(in srgb,var(--dk) 70%,var(--ink))}
.main.board .tc details.dk-more .dk-why{color:var(--mute)}
.main.board{--claude:#d97757}
:root[data-style=dark] .main.board{--claude:#ec9a7c}
.main.board .tc.hl-claude{border-color:var(--claude);box-shadow:0 0 0 2px var(--claude)}
.main.board .tc .hl-tag{display:inline-block;margin:0 0 7px;padding:2px 9px;border-radius:999px;font-size:10.5px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:#fff}
.main.board .tc.hl-claude .hl-tag{background:var(--claude)}
.brand{flex:none;display:flex;align-items:center;gap:7px;padding:7px 10px 0;font-size:14px}
.brand span{font-size:14px;font-weight:600;border-left:1px solid var(--mute);padding-left:7px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#chatline{flex:none;margin:6px 10px 0;padding:5px 9px;border-radius:8px;font-size:12px;line-height:1.35;background:color-mix(in srgb,var(--tint,#2f7cf6) 12%,var(--card));overflow-wrap:anywhere}
.gm{position:absolute;right:-6px;top:-7px;width:16px;height:16px;border-radius:50%;background:#e8eaed;color:#17181a;font:700 10.5px/16px system-ui,sans-serif;text-align:center;box-shadow:0 0 0 1.5px #17181a;z-index:1}
.gm.in{position:static;flex:none;box-shadow:none;background:var(--ink);color:var(--bg)}
.guide .tc{pointer-events:none;margin-top:8px}
.main.board .guide .tc :is(.dk-area,.dk-q,.dk-go,.dk-alt,.b2,.hl-tag,.tc-g,.gfold){position:relative;overflow:visible}
.gfold{display:inline-block;font-size:12px;color:var(--mute);margin-top:8px}
.gfold.right{display:table;margin-left:auto}
.gl{display:grid;gap:7px;margin:8px 2px 14px;font-size:13px;line-height:1.35}
.gl>div{display:flex;gap:8px;align-items:flex-start}
.gl .gm{margin-top:1px}
.main.board .foot #chatmeter{color:var(--ink)!important}
#chatmeter{flex:1 1 100%;padding:0 8px 5px;font-size:12px;line-height:17px}
#chatmeter .cm-t{display:flex;align-items:center;justify-content:space-between;gap:8px;min-height:24px}
.main.board .foot #chatmeter .b2{flex:none;width:auto;margin:0;padding:2px 10px;font-size:12px;cursor:pointer}
.main.board .foot #chatmeter .b2.go{color:#fff;background:#1a73e8;border-color:#1a73e8}
#chatmeter .cm-b{position:relative;height:4px;margin-top:3px;border-radius:2px;background:#5f6368}
#chatmeter .cm-b i{display:block;height:100%;border-radius:2px}
#chatmeter .cm-b u{position:absolute;top:-1px;bottom:-1px;width:1px;background:#e8eaed}
.main.board .tc-row.n3{grid-template-columns:repeat(auto-fit,minmax(84px,1fr))}
.main.board .tc .b2{white-space:normal;overflow:visible}
@media (max-width:290px){.main.board .foot .fbar b{flex-wrap:wrap}.main.board .foot .fbar b i{flex:1 1 40%}}
.main.board .tabs{gap:2px;flex-wrap:wrap}
.main.board .tabs .tab{flex:1 0 auto;padding-left:5px;padding-right:5px}
.main.board .tabs .tab i{min-width:14px;padding:0 4px;margin-left:3px}
.chatnow{font-size:14px;font-weight:600;margin:2px 2px 12px}
.gset{display:grid;gap:10px;margin:6px 2px 14px;font-size:13px}
.gset label{display:grid;gap:3px}
.gset label>span{display:flex;justify-content:space-between;gap:8px}
.gset label>span b{font-weight:600}
.gset input[type=range]{width:100%;margin:0;accent-color:#7c4dff}
.gset select{font:inherit;padding:4px 6px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink)}
.gset label.on{display:flex;justify-content:space-between;align-items:center}
.gset label.on input{width:18px;height:18px;margin:0;accent-color:#7c4dff}
.tabhelp{font-size:12px;line-height:1.35;color:var(--mute);padding:4px 2px 8px}
:root[data-style=claude]{--tint:#d97757}
:root[data-style=claude] .brand b,:root[data-style=claude] .tc-q{font-family:Georgia,'Times New Roman',serif;font-weight:500}
:root[data-style=claude] .tab.on i{background:#c15f3c}
:root[data-style=claude] .gset input{accent-color:#c15f3c}
:root[data-style=claude] .main.board .foot .fbar{background:#30302e}
:root[data-style=claude] .main.board .foot .fbar b i{outline-color:#5e5d59}
'''

BOARD_JS = r'''const TK='boardTab';let tab='now';try{tab=localStorage.getItem(TK)||'now'}catch(e){}if(tab==='recent')tab='test';
function showTab(t,anim){tab=t;try{localStorage.setItem(TK,t)}catch(e){}
 document.querySelectorAll('.tab').forEach(b=>b.classList.toggle('on',b.dataset.t===t));
 document.querySelectorAll('.pane').forEach(p=>{const on=p.dataset.t===t;if(on){p.hidden=false;if(anim){p.classList.remove('in');void p.offsetWidth;p.classList.add('in')}}else p.hidden=true})}
function wireTabs(){document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>showTab(b.dataset.t,true));showTab(tab)}
const K='openFolds';let open=[];try{open=JSON.parse(sessionStorage.getItem(K)||'[]')}catch(e){}
function wireFolds(){document.querySelectorAll('details').forEach(d=>{const s=d.querySelector('summary');if(!s)return;const c=d.closest('[data-card]');const k=(c?c.dataset.card+': ':'')+s.textContent;if(open.includes(k))d.open=true;
 d.addEventListener('toggle',()=>{const st=new Set(open);d.open?st.add(k):st.delete(k);open=[...st];try{sessionStorage.setItem(K,JSON.stringify(open))}catch(e){}})})}
function wireAll(){wireTabs();wireFolds()}
wireAll();
let navAt=0,downAt=0;
document.addEventListener('pointerdown',()=>{downAt=Date.now()},true);
document.addEventListener('click',e=>{const a=e.target.closest&&e.target.closest('a[href]');if(!a)return;const h=a.getAttribute('href');
 if(!h||h[0]==='#'||a.target==='_blank'||e.defaultPrevented)return;navAt=Date.now();a.style.opacity='.55';a.style.pointerEvents='none';setTimeout(()=>{a.style.opacity='';a.style.pointerEvents=''},8000);});
window.addEventListener('pageshow',()=>{navAt=0});
const typing=()=>{const a=document.activeElement;return (a&&/^(TEXTAREA|INPUT|SELECT)$/.test(a.tagName)&&a.type!=='hidden')||[...document.querySelectorAll('textarea')].some(t=>!t.readOnly&&t.value.trim())||!!document.querySelector('input[name=img]')};
function swap(doc){const nm=doc.querySelector('.main');if(!nm)return false;const om=document.querySelector('.main');
 const sc={};om.querySelectorAll('.pane').forEach(p=>sc[p.dataset.t]=p.scrollTop);
 const cnt={};om.querySelectorAll('.tab').forEach(b=>cnt[b.dataset.t]=b.textContent);
 om.replaceWith(nm);wireAll();
 nm.querySelectorAll('.pane').forEach(p=>{if(sc[p.dataset.t])p.scrollTop=sc[p.dataset.t]});
 nm.querySelectorAll('.tab').forEach(b=>{if(cnt[b.dataset.t]!==undefined&&cnt[b.dataset.t]!==b.textContent){const i=b.querySelector('i');if(i)i.classList.add('bump')}});
 followDone();drawChat();syncStyle();
 return true}
const FK='seenDone';
function followDone(){let seen=[];try{seen=JSON.parse(localStorage.getItem(FK)||'[]')}catch(e){}
 const cards=[...document.querySelectorAll('.pane .tc[class*=" hl-"]:not([data-demo])')];
 const key=c=>c.dataset.card+':'+((c.querySelector('.tc-g')||{}).textContent||'').slice(0,80);
 const fresh=cards.find(c=>!seen.includes(key(c)));
 if(fresh){if(Date.now()-downAt<10000)return;
  const p=fresh.closest('.pane');if(p&&p.dataset.t!==tab)showTab(p.dataset.t);fresh.scrollIntoView({block:'nearest'})}
 try{localStorage.setItem(FK,JSON.stringify([...new Set(seen.concat(cards.map(key)))].slice(-60)))}catch(e){}}
followDone();
async function refresh0(){
 try{const r=await fetch(location.pathname,{cache:'no-store'});if(!r.ok)return;const d=new DOMParser().parseFromString(await r.text(),'text/html');if(typing())return;swap(d)}catch(e){}
}
async function refresh(){
 if(Date.now()-navAt<15000||Date.now()-downAt<2000||typing()){setTimeout(refresh,2000);return}
 try{const r=await fetch(location.pathname,{cache:'no-store'});
 if(Date.now()-navAt<15000||Date.now()-downAt<2000||typing()){setTimeout(refresh,2000);return}
  if(!r.ok)throw 0;const d=new DOMParser().parseFromString(await r.text(),'text/html');if(!swap(d))throw 0;document.getElementById('offline').style.display='none';
 }catch(e){document.getElementById('offline').style.display='block'}
 setTimeout(refresh,10000);
}
setTimeout(refresh,10000);
let lastV=null;
setInterval(async()=>{try{const r=await fetch('/v',{cache:'no-store'});if(!r.ok)return;const v=await r.text();
 if(lastV!==null&&v!==lastV&&Date.now()-navAt>=1500&&Date.now()-downAt>=1500&&!typing()){lastV=v;refresh0()}else if(lastV===null)lastV=v;}catch(e){}},2000);

let chatId=new URLSearchParams(location.search).get('chat');try{if(chatId)sessionStorage.setItem('chat',chatId);else chatId=sessionStorage.getItem('chat')}catch(e){}
let chatLast=null;const SW=['Hand-off','Opening','Pinned','Reading','Ready'];
function chatKey(){const e=document.getElementById('chatline');return e?e.dataset.k:''}
function copyCompact(b){try{navigator.clipboard.writeText('/compact')}catch(e){}b.textContent='Copied: paste in chat'}
async function pressNew(b){if(!b.dataset.ok){b.dataset.ok=1;b.textContent='Click again';return}b.textContent='Asking your chat';await fetch('/newchat?k='+chatKey()+'&chat='+encodeURIComponent(chatId||''));chatLine()}
function setChat(el){const v=el.type==='checkbox'?(el.checked?1:0):el.value;fetch('/chatset?k='+chatKey()+'&'+el.name+'='+v).then(()=>chatLine());el.blur()}
function drawChat(){const c=chatLast;if(!c||!c.known)return;const el=document.getElementById('chatline');
 if(el&&chatId){el.hidden=false;el.textContent='This chat'+(c.last?': '+c.last:'')+(c.fresh?' · No new information yet':' · updated '+c.updated)}
 const now=document.getElementById('chatnow');if(now)now.textContent=!c.on?'Chat switching is off':c.pct===null?'This chat: size not known yet':'This chat: '+c.pct+'% full'+(c.compacts?' · compacted '+c.compacts+' of '+c.max:'');
 let bar=document.querySelector('.foot .fbar');if(!bar)return;
 if(c.bar&&chatId&&bar.outerHTML!==c.bar){const t=document.createElement('div');t.innerHTML=c.bar;const n=t.firstChild;if(n.outerHTML!==bar.outerHTML){bar.replaceWith(n);bar=n}}
 ['chatmeter','switchbar'].forEach(i=>{const x=document.getElementById(i);if(x)x.remove()});if(!c.on)return;
 if(c.step!==null&&c.step!==undefined){const d=document.createElement('div');d.id='switchbar';d.className='fbar';d.innerHTML='<span>New chat</span><b>'+SW.map((n,i)=>'<i'+(i===c.step&&i<4?' class=now':'')+(i<=c.step?' style="background:#45a466"':'')+'><em>'+n+'</em></i>').join('')+'</b>';bar.after(d);return}
 const m=document.createElement('div');m.id='chatmeter';const full=c.pct===null?null:c.pct,maxed=c.compacts>=c.max,due=(full!==null&&full>=c.switch_at)||maxed;
 const words=(full===null?'Chat size not known yet':'Chat '+full+'% full')+(c.compacts?' · compacted '+c.compacts+' of '+c.max:'');
 if(!due&&!c.pending&&(full===null||full<c.compact_at||c.compact_mode==='auto'))return;
 const btn=c.pending?'<span>Waiting for your chat. Or say: new chat</span>':due?'<a class="b2 go" onclick="pressNew(this)">New chat</a>':(full!==null&&full>=c.compact_at)?'<a class="b2" onclick="copyCompact(this)">Copy /compact</a>':'';
 m.innerHTML='<div class="cm-t"><span>'+words+'</span>'+btn+'</div><div class="cm-b"><i style="width:'+Math.min(100,full||0)+'%;background:'+(due?'#e0b020':'#45a466')+'"></i><u style="left:'+c.compact_at+'%"></u><u style="left:'+c.switch_at+'%"></u></div>';bar.after(m)}
async function chatLine(){try{const r=await fetch('/chatinfo?chat='+encodeURIComponent(chatId||''),{cache:'no-store'});if(!r.ok)return;chatLast=await r.json();drawChat()}catch(e){}}
function syncStyle(){const s=document.getElementById('stylepick');if(s)s.value=getStyle()}
syncStyle();
chatLine();setInterval(chatLine,2500);
'''


def page():
    return ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Scrumdash</title>' + STYLE_HEAD + '<style>' + CSS + '</style></head><body>'
            + board_html(tickets())
            + '<div id="offline" style="display:none;position:fixed;top:8px;right:8px;background:#f9ab00;color:#000;padding:4px 10px;border-radius:6px;font-size:12px;z-index:5">Reconnecting… (showing last data)</div>'
            '<script>' + BOARD_JS + '</script><script src="/paste.js"></script></body></html>')


PAGE_MAX_AGE = 15
READ_ONLY_PATHS = {'/', '/v', '/paste.js', '/favicon.ico', '/chatinfo', '/sent', '/style.js'}
_PAGE = {'body': None, 'v': None, 't': 0.0, 'gen': 0, 'built': -1}
_PAGE_LOCK = threading.Lock()


def handoffs_version():
    try:
        return max(e.stat().st_mtime_ns for d in (P(), P('tickets')) for e in os.scandir(d) if e.is_file())
    except ValueError:
        return 0


def page_bytes():
    v = handoffs_version()
    with _PAGE_LOCK:
        if _PAGE['body'] is None or _PAGE['v'] != v or _PAGE['built'] != _PAGE['gen'] or time.time() - _PAGE['t'] > PAGE_MAX_AGE:
            gen, t = _PAGE['gen'], time.time()
            _PAGE.update(body=page().encode('utf-8'), v=v, t=t, built=gen)
        return _PAGE['body']


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body=b'', ctype='text/html; charset=utf-8', extra=()):
        self.send_response(code)
        for k, v in extra:
            self.send_header(k, v)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)
        if urllib.parse.urlparse(self.path).path not in READ_ONLY_PATHS:
            _PAGE['gen'] += 1

    def home(self, where='/'):
        return self.send(303, extra=[('Location', where)])

    def small(self, inner):
        return self.send(200, (SMALL_PAGE + inner + '</body></html>').encode())

    def arg(self, name, default=''):
        return self.q.get(name, [default])[0]

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        self.q = urllib.parse.parse_qs(u.query)
        route = {'/v': self.version, '/paste.js': self.paste_js, '/style.js': self.style, '/img': self.img_chunk, '/sent': self.sent, '/chatinfo': self.chatinfo,
                 '/chatset': self.chatset, '/newchat': self.newchat, '/approve': self.approve, '/deny': self.deny, '/notnow': self.not_now, '/answered': self.answered,
                 '/decide': self.decide, '/test': self.test_result, '/already': self.already, '/note': self.note}.get(u.path)
        return route() if route else self.send(200, page_bytes())

    def version(self):
        return self.send(200, f'{handoffs_version()}|{claude_active()}|{code_stamp()}'.encode(), 'text/plain')

    def paste_js(self):
        return self.send(200, PASTE_JS.replace('__K__', sign('img')).encode(), 'application/javascript; charset=utf-8')

    def style(self):
        return self.send(200, style_js().encode(), 'application/javascript')

    def sent(self):
        return self.small(f'<form><b>Sent. Claude reads it and answers in its chat.</b><a href="/" style="{BIG_LINK}">Back</a></form>')

    def chatinfo(self):
        return self.send(200, json.dumps(chat_info(self.arg('chat'))).encode())

    def chatset(self):
        if not signed(self.arg('k'), 'chatset'):
            return self.send(403)
        st = chat_settings()
        for k in ('compact_at', 'switch_at', 'compacts', 'window'):
            if self.arg(k).isdigit():
                st[k] = int(self.arg(k))
        if 'on' in self.q:
            st['on'] = self.arg('on') == '1'
        for k in ('compact_mode', 'new_mode'):
            if self.arg(k) in ('suggest', 'auto'):
                st[k] = self.arg(k)
        sync_compact_setting(st)
        with open(P('CHAT-SETTINGS.json'), 'w', encoding='utf-8') as f:
            json.dump(st, f)
        return self.send(200, b'ok')

    def newchat(self):
        if not signed(self.arg('k'), 'chatset'):
            return self.send(403)
        with open(P('SWITCH-REQUEST.json'), 'w', encoding='utf-8') as f:
            json.dump({'chat': self.arg('chat'), 'at': time.strftime('%Y-%m-%d %H:%M:%S')}, f)
        return self.send(200, b'ok')

    def confirm_page(self, question):
        yes = self.path + '&yes=1'
        return self.small(f'<form><b>{E(question)}</b><a href="{E(yes)}" style="{BIG_LINK}">Yes</a>'
                          '<a href="/" style="text-align:center;padding:8px;color:var(--mute)">Cancel</a></form>')

    def approve(self):
        tid = self.arg('t')
        if waiting(tid) and signed(self.arg('k'), 'build:' + tid):
            if 'yes' not in self.q:
                return self.confirm_page(title_of(tid, 'This') + ': build it?')
            record(ticket=tid)
            mark_approved(tid)
        return self.home()

    def not_now(self):
        tid = self.arg('t')
        if waiting(tid) and signed(self.arg('k'), 'notnow:' + tid):
            subprocess.run([sys.executable, os.path.join(TOOLS, 'ticket.py'), 'parked', tid, 'pressed Not now on the board'], cwd=ROOT, capture_output=True, timeout=60,
                           creationflags=0x08000000 if os.name == 'nt' else 0)
            record(notnow=tid, started=True)
        return self.home()

    def already(self):
        tid = self.arg('t')
        if waiting(tid) and signed(self.arg('k'), 'already:' + tid):
            if 'yes' not in self.q:
                return self.confirm_page(title_of(tid, 'This') + ': already works? It is closed without building.')
            already_works(tid)
        return self.home()

    def deny(self):
        tid = self.arg('t')
        if waiting(tid) and signed(self.arg('k'), 'deny:' + tid):
            if 'yes' not in self.q:
                return self.confirm_page(title_of(tid, 'This') + ': drop it?')
            deny_ticket(tid)
        return self.home()

    def answered(self):
        h = self.arg('h')
        if re.fullmatch(r'[0-9a-f]{12}', h) and signed(self.arg('k'), 'answered:' + h):
            mark_question_answered(h)
        return self.home()

    def decide(self):
        did, oi = self.arg('d'), self.arg('o', '-1')
        d = next((x for x in decisions() if x['id'] == did), None)
        if d and oi.isdigit() and int(oi) < len(d['options']) and signed(self.arg('k'), 'dec:' + did + ':' + oi) and did not in chosen() and not d['chosen']:
            choice = d['options'][int(oi)]
            if 'yes' not in self.q:
                return self.confirm_page(f'{d["title"]}: choose "{choice}"?')
            record(decision=did, choice=choice)
            decision_record(did, choice)
        return self.home()

    def test_result(self):
        tid, r = self.arg('t'), self.arg('r')
        if self.arg('sub') == '1':
            return self.test_note()
        if r in ('works', 'fails', 'partly', 'feedback') and signed(self.arg('k'), 'test:' + tid + ':' + r):
            title = title_of(tid, tid)
            if r in ('fails', 'partly'):
                head, ask = ('Works partly', 'What works, and what doesn&#39;t yet?') if r == 'partly' else ('Doesn&#39;t work', 'What did you see, and where?')
                return self.small(f'<form method="get" action="/test" style="width:min(560px,calc(100vw - 60px))"><input type="hidden" name="sub" value="1"><b>{head}: {E(title)}</b>'
                                  f'<div>{ask}</div>'
                                  f'<input type="hidden" name="t" value="{E(tid)}"><input type="hidden" name="r" value="{r}"><input type="hidden" name="k" value="{E(self.arg("k"))}">' + NOTE_BOX)
            if 'yes' not in self.q:
                return self.confirm_page(title_of(tid, 'This') + (': you gave your feedback in chat?' if r == 'feedback' else ': does it work?'))
            record(test=tid, result=r)
            mark_tested(tid, r)
        return self.home()

    def test_note(self):
        tid, text = self.arg('t'), with_image(self.q, self.arg('x').strip()[:4000])
        r = 'partly' if self.arg('r') == 'partly' else 'fails'
        if text and signed(self.arg('k'), 'test:' + tid + ':' + r):
            record(test=tid, result=r, why=text)
            mark_tested(tid, r)
            return self.home('/sent')
        return self.home()

    def note(self):
        tid = self.arg('t')
        if not ((tid == 'new' or any(h.split()[0] == tid for h in tickets())) and signed(self.arg('k'), 'note:' + tid)):
            return self.home()
        if self.arg('sub') == '1':
            text = with_image(self.q, self.arg('x').strip()[:4000])
            if not text:
                return self.home()
            kind = self.arg('kind', 'chat')
            kind = kind if kind in ('build', 'chat') else 'chat'
            n = ticket_notes.add(tid, text, kind)
            record(note=tid, id=n['id'], kind=kind)
            return self.home('/sent')
        title = 'Tell Claude: what should be built, fixed or talked about?' if tid == 'new' else 'Add to: ' + title_of(tid, tid)
        return self.small(f'<form method="get" action="/note" style="width:min(560px,calc(100vw - 60px))"><input type="hidden" name="sub" value="1"><b>{E(title)}</b>'
                          f'<input type="hidden" name="t" value="{E(tid)}"><input type="hidden" name="k" value="{E(self.arg("k"))}">'
                          '<div style="display:grid;gap:4px;font-size:14px">'
                          '<label><input type="radio" name="kind" value="build"' + (' checked' if tid == 'new' else '') + '> 🔨 Build this</label>'
                                                    '<label><input type="radio" name="kind" value="chat"' + ('' if tid == 'new' else ' checked') + '> 💬 Just talk about it</label></div>' + NOTE_BOX)

    def img_chunk(self):
        iid, i, n, d = self.arg('id'), self.arg('i'), self.arg('n'), self.arg('d')
        if not (signed(self.arg('k'), 'img') and re.fullmatch(r'[0-9a-f]{12}', iid) and i.isdigit() and n.isdigit() and 0 < int(n) <= 800 and int(i) < int(n)):
            return self.send(400, b'{"ok":false}', 'application/json')
        with IMG_LOCK:
            parts = IMG_PARTS.setdefault(iid, {})
            parts[int(i)] = d
            done = len(parts) == int(n)
            if done:
                del IMG_PARTS[iid]
        if done:
            try:
                raw = base64.b64decode(''.join(parts[k] for k in range(int(n))))
            except Exception:
                return self.send(400, b'{"ok":false}', 'application/json')
            if not raw.startswith(b'\xff\xd8') or len(raw) > 4_000_000:
                return self.send(400, b'{"ok":false}', 'application/json')
            os.makedirs(IMG_DIR, exist_ok=True)
            with open(os.path.join(IMG_DIR, iid + '.jpg'), 'wb') as f:
                f.write(raw)
        return self.send(200, json.dumps({'ok': True, 'done': done}).encode(), 'application/json')


class Server(ThreadingHTTPServer):
    allow_reuse_address = os.name != 'nt'   # on Windows it would let two boards share a port


CODE_FILES = [os.path.abspath(__file__), os.path.join(TOOLS, 'ticket_notes.py')]


def code_stamp():
    try:
        return max(os.path.getmtime(f) for f in CODE_FILES)
    except OSError:
        return 0


def watch_code(server, started):
    """An open board picks up a new version by itself, so no one is left looking at old code. Half-copied or broken code is ignored."""
    while True:
        time.sleep(3)
        try:
            stamp = code_stamp()
            if not stamp or stamp == started:
                continue
            time.sleep(2)
            if code_stamp() != stamp:
                continue
            for f in CODE_FILES:
                compile(open(f, encoding='utf-8').read(), f, 'exec')
        except (OSError, SyntaxError, ValueError):
            continue
        return server.shutdown()


if __name__ == '__main__':
    if not os.environ.get('SCRUMDASH_PORT'):
        sys.exit('Start the board with: python app.py <project folder>')
    try:
        srv = Server(('127.0.0.1', int(os.environ['SCRUMDASH_PORT'])), Handler)
    except OSError as e:
        print('this port is in use:', e)
        sys.exit(0)
    threading.Thread(target=watch_code, args=(srv, code_stamp()), daemon=True).start()
    srv.serve_forever()
    srv.server_close()
    sys.modules.pop('ticket_notes', None)
    runpy.run_path(os.path.abspath(__file__), run_name='__main__')
