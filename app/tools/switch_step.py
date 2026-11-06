"""Shows the new-chat steps on the board. python tools/switch_step.py <0-4>   (0 Hand-off, 1 Opening, 2 Pinned, 3 Reading, 4 Ready)"""
import json, os, sys, time
sys.stdout.reconfigure(encoding='utf-8', errors='replace')   # a Windows pipe cannot show every letter otherwise
if len(sys.argv) < 2 or sys.argv[1] not in ('0', '1', '2', '3', '4'):
    sys.exit(__doc__)
H = os.path.join(os.environ.get('SCRUMDASH_PROJECT') or os.getcwd(), 'handoffs')
json.dump({'step': int(sys.argv[1]), 't': time.time()}, open(os.path.join(H, 'SWITCH-STATE.json'), 'w', encoding='utf-8'))
print('New chat step', sys.argv[1])
