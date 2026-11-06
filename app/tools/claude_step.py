"""Shows Claude's step on the board. python tools/claude_step.py "<task>" <0-3> [Tn] | clear"""
import json, os, re, sys, time
sys.stdout.reconfigure(encoding='utf-8', errors='replace')   # a Windows pipe cannot show every letter otherwise

F = os.path.join(os.environ.get('SCRUMDASH_PROJECT') or os.getcwd(), 'handoffs', 'CLAUDE-PROGRESS.json')
if len(sys.argv) < 2:
    sys.exit(__doc__)
if sys.argv[1] == 'clear':
    data = {}
else:
    data = {'live': True, 'task': sys.argv[1], 'step': int(sys.argv[2]) if len(sys.argv) > 2 else 0, 'ticket': sys.argv[3] if len(sys.argv) > 3 else '', 'at': time.strftime('%Y-%m-%d %H:%M')}

# The project's newest step, plus one per chat: chats in one folder must not overwrite each other's line.
files = [F]
chat = os.environ.get('CLAUDE_CODE_SESSION_ID', '')
if re.fullmatch(r'[0-9a-zA-Z-]{8,64}', chat):
    os.makedirs(os.path.join(os.path.dirname(F), 'chat-steps'), exist_ok=True)
    files.append(os.path.join(os.path.dirname(F), 'chat-steps', chat + '.json'))
for name in files:
    for attempt in range(5):
        try:
            json.dump(data, open(name + '.tmp', 'w', encoding='utf-8'), ensure_ascii=False)
            os.replace(name + '.tmp', name)
            break
        except OSError:
            time.sleep(0.3)
print('Claude bar:', data or 'cleared')
