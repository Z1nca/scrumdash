import json, os, sys, time

FILE = os.path.join(os.environ.get('SCRUMDASH_PROJECT') or os.getcwd(), 'handoffs', 'TICKET-NOTES.jsonl')

def all_notes():
    out = []
    try:
        with open(FILE, encoding='utf-8') as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return out

def for_ticket(tid):
    return [n for n in all_notes() if n.get('ticket') == tid]

def add(tid, text, kind='chat'):

    n = {'id': 'U' + time.strftime('%d%H%M%S'), 'ticket': tid, 'kind': kind, 'text': text, 'at': time.strftime('%Y-%m-%d %H:%M')}
    with open(FILE, 'a', encoding='utf-8') as f:
        f.write(json.dumps(n, ensure_ascii=False) + '\n')
    return n

def move(nid, tid):
    notes = all_notes()
    hit = [n for n in notes if n.get('id') == nid]
    for n in hit:
        n['ticket'] = tid
    tmp = FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.writelines(json.dumps(n, ensure_ascii=False) + '\n' for n in notes)
    os.replace(tmp, FILE)
    return bool(hit)

if __name__ == '__main__':
    a = sys.argv[1:]
    if len(a) == 3 and a[0] == 'move':
        print('moved' if move(a[1], a[2]) else 'note not found'); sys.exit(0)
    for n in (for_ticket(a[0]) if a else all_notes()):
        print(f"{n['id']}  {n['ticket']:4}  {n['at']}  {n['text']}")
