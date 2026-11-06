"""Waits until the person presses New chat on the board for this chat, then says so and exits. python tools/wait_switch.py <chat id>"""
import json, os, sys, time
sys.stdout.reconfigure(encoding='utf-8', errors='replace')   # a Windows pipe cannot show every letter otherwise
H = os.path.join(os.environ.get('SCRUMDASH_PROJECT') or os.getcwd(), 'handoffs')
F = os.path.join(H, 'SWITCH-REQUEST.json')
chat = sys.argv[1] if len(sys.argv) > 1 else ''
while True:
    try:
        wanted = json.load(open(F, encoding='utf-8')).get('chat', '')
    except (OSError, ValueError):
        wanted = None
    if wanted is not None and (not wanted or not chat or wanted == chat):
        os.remove(F)
        print('The person pressed New chat on the board. Follow the New chat steps in the Scrumdash block now.')
        break
    time.sleep(2)
