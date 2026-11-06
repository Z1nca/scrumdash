"""Scrumdash: python app.py (opens the start menu) | python app.py <project folder> [--port N] | python app.py setup [folder] | python app.py hook | python app.py join <chat id>"""
import contextlib, glob, io, json, os, re, shutil, socket, subprocess, sys, threading, time, urllib.parse, webbrowser, zlib
APP = os.path.dirname(os.path.abspath(__file__)).replace('\\', '/')


def python_works(name):
    path = shutil.which(name)
    if not path:
        return False
    if 'windowsapps' not in path.lower():
        return True
    try:
        return subprocess.run([path, '--version'], capture_output=True, timeout=10, creationflags=0x08000000).returncode == 0   # no console window
    except (OSError, subprocess.SubprocessError):
        return False


def own_python():
    """The Python inside the Windows download, as command text that works unquoted in every shell a chat may use."""
    path = os.path.join(APP, 'python', 'python.exe')
    if not os.path.exists(path):
        return ''
    import ctypes
    short = ctypes.create_unicode_buffer(1024)
    if ctypes.windll.kernel32.GetShortPathNameW(path, short, 1024):
        path = short.value
    path = path.replace('\\', '/')
    return f'"{path}"' if ' ' in path else path


PY = own_python() or next((x for x in ('python', 'python3', 'py') if python_works(x)), 'python')


def port_of(project):
    return 8800 + zlib.crc32(os.path.abspath(project).lower().encode()) % 20000


def prepare(project):
    os.makedirs(os.path.join(project, 'handoffs', 'tickets'), exist_ok=True)


OLD = 'claudedex'   # this app's name until 6 Nov 2026: what it left behind is carried over or tidied


def carry_over():
    """Home files from before the rename keep working: the key that signs board buttons, recent folders, the chosen style."""
    for name in ('-secret', '-recent.json', '-style.json'):
        old, new = os.path.expanduser('~/.' + OLD + name), os.path.expanduser('~/.scrumdash' + name)
        try:
            if os.path.exists(old) and not os.path.exists(new):
                shutil.copyfile(old, new)
        except OSError:
            pass


def backup(p):
    if os.path.exists(p) and not os.path.exists(p + '.before-scrumdash') and not os.path.exists(p + '.before-' + OLD):
        shutil.copyfile(p, p + '.before-scrumdash')


def add_block(project, name, url, pane):
    p = os.path.join(project, name)
    block = open(os.path.join(APP, 'INSTRUCTIONS-FOR-AI.md'), encoding='utf-8').read().replace('{url}', url).replace('{pane}', pane)
    block = re.sub(r'`tools/(\w+\.py)', lambda m: f'`{PY} "{APP}/tools/{m.group(1)}"', block)
    old = open(p, encoding='utf-8').read() if os.path.exists(p) else ''
    old = old.replace(f'<!-- {OLD} start -->', '<!-- scrumdash start -->').replace(f'<!-- {OLD} end -->', '<!-- scrumdash end -->')
    backup(p)
    after = ''
    if '<!-- scrumdash start -->' in old:
        end = old.find('<!-- scrumdash end -->')
        after = old[end + len('<!-- scrumdash end -->'):].strip() if end >= 0 else old[old.index('<!-- scrumdash start -->') + len('<!-- scrumdash start -->'):].strip()
        old = old[:old.index('<!-- scrumdash start -->')].rstrip()
    with open(p, 'w', encoding='utf-8') as f:
        f.write((old + '\n\n' if old else '') + block.rstrip() + ('\n\n' + after + '\n' if after else '\n'))


def read_json(p, empty):
    return json.load(open(p, encoding='utf-8-sig')) if os.path.exists(p) else empty


def was_ours(command, word):
    """A step left by a Scrumdash that moved: its folder is gone, or it holds Scrumdash. Another tool's app.py stays."""
    m = re.search(r'"([^"]*)/app\.py" ' + word + '$', command)
    return bool(m) and (not os.path.isdir(m.group(1)) or os.path.exists(os.path.join(m.group(1), 'tools', 'board.py')))


def setup(project):
    if os.path.normcase(os.path.abspath(project)) == os.path.normcase(os.path.abspath(os.path.expanduser('~'))):
        raise ValueError('That is your home folder. Scrumdash would land in every chat on this computer, so choose a project folder inside it.')
    prepare(project)
    port = port_of(project)
    url = f'http://127.0.0.1:{port}/'
    add_block(project, 'CLAUDE.md', url, ' in your browser pane: preview_start with the name "scrumdash", then navigate it to the address the start-of-chat note gives you')
    d = os.path.join(project, '.claude')
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, 'launch.json')
    backup(p)
    data = read_json(p, {'version': '0.0.1'})
    data['configurations'] = [c for c in data.get('configurations', []) if c.get('name') not in ('scrumdash', OLD)] + [
        {'name': 'scrumdash', 'runtimeExecutable': PY.strip('"'), 'runtimeArgs': [APP + '/app.py', '.', '--port', str(port)], 'port': port}]
    json.dump(data, open(p, 'w', encoding='utf-8'), indent=2)
    p = os.path.join(d, 'settings.json')
    backup(p)
    data = read_json(p, {})
    for event, word in (('SessionStart', 'hook'), ('UserPromptSubmit', 'nudge'), ('PreToolUse', 'track'), ('Stop', 'track'), ('Stop', 'stopcheck'), ('PreCompact', 'hold')):
        hooks = data.setdefault('hooks', {}).setdefault(event, [])
        cmd = f'{PY} "{APP}/app.py" {word}'
        for e in hooks:   # Scrumdash may have moved: a step from its old folder would fail
            e['hooks'] = [h for h in e.get('hooks', []) if h.get('command') == cmd or not was_ours(h.get('command', ''), word)]
        hooks[:] = [e for e in hooks if e.get('hooks')]
        if not any(h.get('command') == cmd for e in hooks for h in e.get('hooks', [])):
            # the bar's steps run alongside Claude, so they can never slow a chat down
            hooks.append({'hooks': [{'type': 'command', 'command': cmd, **({'async': True} if word == 'track' else {})}]})
    json.dump(data, open(p, 'w', encoding='utf-8'), indent=2)
    install_command()
    print(f'Scrumdash is set up in {project}.\nBoard address: {url}\nStart a new chat there. Files it changed keep a copy ending in .before-scrumdash.')


READING = {'Read', 'Glob', 'Grep', 'LS', 'WebFetch', 'WebSearch', 'ToolSearch'}
BUILDING = {'Edit', 'Write', 'MultiEdit', 'NotebookEdit'}
SHELLS = {'Bash', 'PowerShell'}
LOOKS = re.compile(r'^\s*(ls|dir|cat|type|head|tail|grep|rg|find|pwd|git (status|log|diff|show)|get-childitem|get-content|select-string)\b', re.I)
CHECKS = re.compile(r'\b(test|tests|pytest|check|verify|lint|tsc)\b', re.I)


def step_of(tool, data):
    """Which stage a real action belongs to. None leaves the bar as it is."""
    if tool in READING:
        return 0
    if tool in BUILDING:
        return 1
    if tool in SHELLS:
        command = str((data or {}).get('command', ''))
        if re.search(r'tools[/\\](claude_step|ticket|decide|switch_step|wait_switch)\.py', command):
            return None
        return 2 if CHECKS.search(command) else 0 if LOOKS.search(command) else 1
    if 'browser' in tool.lower():
        return 2
    return None


def save_step(project, cid, change):
    """Updates the chat's own line and the project's newest line, keeping what Claude said it is doing."""
    h = os.path.join(project, 'handoffs')
    if not os.path.isdir(h) or not re.fullmatch(r'[0-9a-zA-Z-]{8,64}', cid or ''):
        return
    os.makedirs(os.path.join(h, 'chat-steps'), exist_ok=True)
    for p in (os.path.join(h, 'chat-steps', cid + '.json'), os.path.join(h, 'CLAUDE-PROGRESS.json')):
        try:
            data = json.load(open(p, encoding='utf-8'))
        except (OSError, ValueError):
            data = {}
        data.update(change)
        data['at'] = time.strftime('%Y-%m-%d %H:%M:%S')
        try:
            with open(p + '.tmp', 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(p + '.tmp', p)
        except OSError:
            pass


def track(info):
    project = os.environ.get('CLAUDE_PROJECT_DIR') or info.get('cwd') or os.getcwd()
    cid, event = info.get('session_id', ''), info.get('hook_event_name')
    if event == 'UserPromptSubmit':
        save_step(project, cid, {'task': '', 'step': 0, 'live': True})
    elif event == 'Stop':
        save_step(project, cid, {'live': False})
    elif event == 'PreToolUse':
        step = step_of(info.get('tool_name', ''), info.get('tool_input'))
        save_step(project, cid, {'live': True} if step is None else {'step': step, 'live': True})


HOLD_LIMIT = 90   # above this a held compact goes ahead, so a chat never runs out of room


def chat_options(project):
    try:
        d = json.load(open(os.path.join(project, 'handoffs', 'CHAT-SETTINGS.json'), encoding='utf-8'))
    except (OSError, ValueError):
        d = {}
    return d if isinstance(d, dict) else {}


def fullness(transcript, window):
    """How full a chat is, in percent, from the newest size in its record. None when not known."""
    used = 0
    try:
        with open(transcript, 'rb') as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 400000))
            for line in f.read().decode('utf-8', 'replace').splitlines():
                if '"usage"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                u = (o.get('message') or {}).get('usage') or {}
                if u and not o.get('isSidechain'):
                    used = sum(int(u.get(k) or 0) for k in ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'))
    except (OSError, TypeError):
        return None
    if not used:
        return None
    return round(100 * used / (1000000 if used > window else window))


def window_of(options):
    return 200000 if str(options.get('window')) == '200000' else 1000000


def hold(info):
    """Automatic compacting: Claude's own compact waits while Claude is building or checking, and goes ahead at the next good moment."""
    project = os.environ.get('CLAUDE_PROJECT_DIR') or info.get('cwd') or os.getcwd()
    options = chat_options(project)
    if info.get('trigger') != 'auto' or options.get('compact_mode') != 'auto':
        return
    p = os.path.join(project, 'handoffs', 'chat-steps', str(info.get('session_id', '')) + '.json')
    try:
        step = json.load(open(p, encoding='utf-8'))
        busy = step.get('live') and step.get('step') in (1, 2) and time.time() - os.path.getmtime(p) < 900
    except (OSError, ValueError):
        return
    full = fullness(info.get('transcript_path', ''), window_of(options))
    if busy and full is not None and full < HOLD_LIMIT:
        print('Scrumdash: waiting for Claude to finish this step before compacting.', file=sys.stderr)
        sys.exit(2)


def stopcheck(info):
    """Automatic New chat: Claude has just finished, so this is a good moment. Asks once per chat."""
    project = os.environ.get('CLAUDE_PROJECT_DIR') or info.get('cwd') or os.getcwd()
    options = chat_options(project)
    if info.get('stop_hook_active') or options.get('on', True) is not True or options.get('new_mode') != 'auto':
        return
    p, cid = os.path.join(project, 'handoffs', 'CHATS.json'), info.get('session_id', '')
    try:
        chats = json.load(open(p, encoding='utf-8'))
    except (OSError, ValueError):
        return
    chat = chats.get(cid)
    if not isinstance(chat, dict) or chat.get('auto_new'):
        return
    full = fullness(info.get('transcript_path') or chat.get('transcript', ''), window_of(options))
    if (full is not None and full >= int(options.get('switch_at', 70))) or int(chat.get('compacts', 0)) >= int(options.get('compacts', 2)):
        chat['auto_new'] = True
        json.dump(chats, open(p, 'w', encoding='utf-8'), indent=1)
        print(json.dumps({'decision': 'block', 'reason': 'Scrumdash: this chat is full enough and you just finished, and the person turned on automatic New chat. Follow the New chat steps in the Scrumdash block in CLAUDE.md now.'}))


def hook_input():
    try:
        return json.loads(sys.stdin.buffer.read().decode('utf-8'))
    except (ValueError, AttributeError, OSError):
        return {}


def claude_home():
    return os.environ.get('CLAUDE_CONFIG_DIR') or os.path.expanduser('~/.claude')


def install_command():
    """/scrumdash, for every chat on this computer. Somebody's own command of that name stays untouched. A failure here must not stop the rest."""
    p = os.path.join(claude_home(), 'commands', 'scrumdash.md')
    try:
        if os.path.exists(p) and COMMAND_MARK.encode() not in open(p, 'rb').read():
            return
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            f.write(f'''---
description: Open the Scrumdash board next to this chat
---
{COMMAND_MARK}
Run this command, then do exactly what it prints. Say in one short line that the board is open.

`{PY} "{APP}/app.py" join "${{CLAUDE_SESSION_ID}}"`
''')
        old = os.path.join(claude_home(), 'commands', OLD + '.md')   # the command from before the rename, only if this app made it
        if os.path.exists(old) and b'<!-- made by ' + OLD.capitalize().encode() + b' -->' in open(old, 'rb').read():
            os.remove(old)
    except OSError:
        pass


def remember_chat(project, cid, transcript, source=''):
    prepare(project)
    p = os.path.join(project, 'handoffs', 'CHATS.json')
    chats = json.load(open(p, encoding='utf-8')) if os.path.exists(p) else {}
    chats.setdefault(cid, {'start': time.time()})['transcript'] = transcript
    if source == 'compact':
        chats[cid]['compacts'] = chats[cid].get('compacts', 0) + 1
    json.dump(chats, open(p, 'w', encoding='utf-8'), indent=1)


def chat_folder(transcript):
    """The folder a chat started in, from its own record: the shell may have moved into a subfolder since."""
    try:
        with open(transcript, encoding='utf-8') as f:
            for _, line in zip(range(50), f):
                folder = json.loads(line).get('cwd')
                if folder and os.path.isdir(folder):
                    return folder
    except (OSError, ValueError):
        pass
    return os.getcwd()


def switch_line(project, cid):
    on = chat_options(project).get('on', True) is True
    return f'Chat switching is on: run this in the background now, and again after it ends if you stay in this chat: {PY} "{APP}/tools/wait_switch.py" {cid}' if on else ''


def join(cid):
    """/scrumdash: connects the chat it is typed in. The chat already knows its own folder, so nothing is asked."""
    known = bool(re.fullmatch(r'[0-9a-fA-F-]{36}', cid or ''))
    found = glob.glob(os.path.join(glob.escape(claude_home()), 'projects', '*', cid + '.jsonl')) if known else []
    project = chat_folder(found[0]) if found else os.getcwd()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            setup(project)
    except (ValueError, OSError) as e:
        return print(f'Tell the person, in plain words: Scrumdash could not connect this chat. {e}')
    url = f'http://127.0.0.1:{port_of(project)}/'
    if known:
        remember_chat(project, cid, found[0] if found else '')
        url += '?chat=' + cid
        if switch_line(project, cid):
            print(switch_line(project, cid))
    print(f'Scrumdash is connected to {project}.\n1. Read the "Scrumdash board" block in CLAUDE.md in that folder and follow it from now on.\n'
          f'2. Open the board: preview_start with the name "scrumdash", then navigate that tab to {url}\nNo browser pane here? Give the person that address to open.')


def hook():
    info = json.loads(sys.stdin.buffer.read().decode('utf-8'))
    project = info.get('cwd') or os.getcwd()
    cid = info.get('session_id', '')
    remember_chat(project, cid, info.get('transcript_path', ''), info.get('source', ''))
    print(f'Scrumdash board for this chat: preview_start with the name "scrumdash", then navigate that tab to http://127.0.0.1:{port_of(project)}/?chat={cid}')
    if switch_line(project, cid):
        print(switch_line(project, cid))


def running(port):
    with socket.socket() as c:
        c.settimeout(0.5)
        return c.connect_ex(('127.0.0.1', int(port))) == 0


def start_board(project, port):
    """The board for this folder, unless it already runs."""
    if running(port):
        return None
    flags = 0x08000000 if os.name == 'nt' else 0
    py = sys.executable
    if os.path.basename(py).lower() == 'pythonw.exe':
        py = os.path.join(os.path.dirname(py), 'python.exe')
    return subprocess.Popen([py, os.path.join(APP, 'tools', 'board.py')], cwd=project, env=dict(os.environ, SCRUMDASH_PROJECT=project, SCRUMDASH_PORT=str(port)),
                            creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


RECENT = os.path.expanduser('~/.scrumdash-recent.json')
COMMAND_MARK = '<!-- made by Scrumdash -->'
FIRST_MESSAGE = 'Please open the Scrumdash board next to this chat.'
STYLE_FILE = os.path.expanduser('~/.scrumdash-style.json')
STYLES = ('dark', 'light', 'claude')
WINDOWS_NAMES = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}


def style_saved():
    try:
        v = json.load(open(STYLE_FILE, encoding='utf-8')).get('style')
    except (OSError, ValueError, AttributeError):
        v = None
    return v if v in STYLES else 'auto'


def style_save(key):
    json.dump({'style': key, 'at': int(time.time())}, open(STYLE_FILE, 'w', encoding='utf-8'))


def open_new_chat(project):
    """Asks the Claude app for a new chat in this folder, first message typed but not sent. The app asks to confirm the folder. False when nothing here opens such a link."""
    url = 'claude://code/new?folder=' + urllib.parse.quote(project, safe='') + '&q=' + urllib.parse.quote(FIRST_MESSAGE, safe='')
    try:
        if os.name == 'nt':
            os.startfile(url)
            return True
        return subprocess.run(['open' if sys.platform == 'darwin' else 'xdg-open', url], capture_output=True).returncode == 0
    except OSError:
        return False


def new_home():
    """Where a new project goes: the person's Documents folder."""
    d = os.path.abspath(os.path.expanduser('~/Documents'))
    return d if os.path.isdir(d) else os.path.abspath(os.path.expanduser('~'))


def name_problem(name):
    if not name:
        return 'Give your project a name first.'
    if len(name) > 60 or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name[-1] in '. ' or name.split('.')[0].upper() in WINDOWS_NAMES:
        return 'That name cannot be a folder name. Try letters, numbers and spaces.'
    if os.path.exists(os.path.join(new_home(), name)):
        return 'That folder already exists. Use "I already have a project" instead.'
    return ''


def recent(add=None):
    try:
        items = json.load(open(RECENT, encoding='utf-8'))
    except (OSError, ValueError):
        items = []
    items = [x for x in items if os.path.isdir(x)]
    if add:
        items = ([add] + [x for x in items if x != add])[:5]
        json.dump(items, open(RECENT, 'w', encoding='utf-8'))
    return items


def app_browser():
    """A browser that can show a page as a small window of its own, without its bars."""
    base = [os.environ.get(k, '') for k in ('ProgramFiles(x86)', 'ProgramFiles', 'LocalAppData')]
    spots = [os.path.join(b, p) for b in base if b for p in (r'Microsoft\Edge\Application\msedge.exe', r'Google\Chrome\Application\chrome.exe')]
    spots += ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge']
    found = [p for p in spots if os.path.exists(p)]
    return found[0] if found else next(filter(None, map(shutil.which, ('msedge', 'google-chrome', 'chromium', 'chromium-browser', 'microsoft-edge'))), None)


PICK_FOLDER = ("import sys,tkinter,tkinter.filedialog as d\n"
               "try:\n import ctypes;ctypes.windll.shcore.SetProcessDpiAwareness(1)\nexcept Exception:\n pass\n"
               "r=tkinter.Tk();r.withdraw();r.attributes('-topmost',True);print(d.askdirectory(parent=r,title='Choose your project folder'))")


def pick_folder():
    exe = os.path.join(os.path.dirname(APP), 'Scrumdash.exe')
    if os.name == 'nt' and os.path.exists(exe):   # the normal Windows folder window, which works without tkinter
        try:
            out = subprocess.run([exe, '--pick-folder'], capture_output=True, encoding='utf-8')
            if out.returncode == 0:
                return {'path': os.path.abspath(out.stdout.strip()) if out.stdout.strip() else ''}
        except OSError:
            pass
    py = sys.executable
    if os.path.basename(py).lower() == 'pythonw.exe':
        py = os.path.join(os.path.dirname(py), 'python.exe')
    try:
        out = subprocess.run([py, '-c', PICK_FOLDER], capture_output=True, encoding='utf-8', env=dict(os.environ, PYTHONIOENCODING='utf-8'),
                             creationflags=0x08000000 if os.name == 'nt' else 0)
    except OSError:
        return {'ask': True}
    return {'ask': True} if out.returncode else {'path': os.path.abspath(out.stdout.strip()) if out.stdout.strip() else ''}


def no_maximize():
    """The start menu is a small fixed menu: it can only be minimized or closed. Windows only; elsewhere nothing happens."""
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
    except (ImportError, AttributeError):
        return
    def check(h, _):
        title, kind = ctypes.create_unicode_buffer(64), ctypes.create_unicode_buffer(64)
        u.GetWindowTextW(h, title, 64)
        u.GetClassNameW(h, kind, 64)
        if title.value == 'Scrumdash' and kind.value == 'Chrome_WidgetWin_1':
            style = u.GetWindowLongW(h, -16)
            if style & 0x00010000:
                u.SetWindowLongW(h, -16, style & ~0x00010000)
                u.SetWindowPos(h, 0, 0, 0, 0, 0, 0x27)
            if u.IsZoomed(h):
                u.ShowWindow(h, 9)
        return True
    u.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(check), 0)


def start_menu():
    """The first screen: the same look as the board, in a small app window. The board itself opens in Claude's browser pane."""
    import secrets
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    install_command()
    key, state = secrets.token_hex(16), {'proc': None, 'board': '', 'project': '', 'chat': False, 'seen': time.time(), 'closed': 0}
    def view():
        return {'style': style_saved(), 'home': new_home(), 'project': state['project'], 'name': os.path.basename(state['project']) or state['project'], 'chat': state['chat'],
                'recent': [{'name': os.path.basename(x) or x, 'path': x} for x in recent()]}
    def create(name):
        name = name.strip()
        problem = name_problem(name)
        if problem:
            return {'error': problem}
        project = os.path.join(new_home(), name)
        try:
            os.makedirs(project)
        except OSError:
            return {'error': 'That folder could not be made here.'}
        return connect(project)
    def connect(project):
        if not os.path.isdir(project):
            return {'error': 'That folder does not exist.'}
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                setup(project)
        except ValueError as e:   # includes a settings file that is not readable
            return {'error': str(e) if 'home folder' in str(e) else 'Scrumdash could not read the settings files in that folder (.claude/settings.json or .claude/launch.json).'}
        except OSError:
            return {'error': 'Scrumdash could not write in that folder.'}
        recent(project)
        if state['proc'] and state['board'] != project:
            state['proc'].terminate()
            state['proc'].wait(5)
            state['proc'] = None
        if not (state['proc'] and state['proc'].poll() is None):
            state['proc'] = start_board(project, port_of(project))
        state['board'] = project
        state['project'] = project
        state['chat'] = open_new_chat(project)
        return view()
    class Menu(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass
        def reply(self, body, kind='application/json'):
            self.send_response(200)
            self.send_header('Content-Type', kind)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            path, _, query = self.path.partition('?')
            if path == '/logo.svg':
                return self.reply(open(os.path.join(APP, 'logo.svg'), 'rb').read(), 'image/svg+xml')
            if path == '/' and query == 'k=' + key:
                return self.reply(open(os.path.join(APP, 'start.html'), 'rb').read(), 'text/html; charset=utf-8')
            self.send_error(404)
        def do_POST(self):
            path, _, query = self.path.partition('?')
            if query != 'k=' + key:
                return self.send_error(403)
            state['seen'], state['closed'] = time.time(), 0
            try:
                data = json.loads(self.rfile.read(min(int(self.headers.get('Content-Length') or 0), 8000)) or '{}')
            except ValueError:
                data = {}
            if path == '/api/style' and data.get('style') in ('auto', *STYLES):
                style_save(data['style'])
            elif path == '/api/choose':
                return self.reply(json.dumps(pick_folder()).encode())
            elif path == '/api/connect':
                return self.reply(json.dumps(connect(os.path.abspath(str(data.get('path', ''))))).encode())
            elif path == '/api/new':
                return self.reply(json.dumps(create(str(data.get('name', '')))).encode())
            elif path == '/api/chat' and state['project']:
                state['chat'] = open_new_chat(state['project'])
            elif path == '/api/another':
                state['project'] = ''
            elif path == '/api/closed':
                state['closed'] = time.time()
            self.reply(json.dumps(view()).encode())
    srv = ThreadingHTTPServer(('127.0.0.1', 0), Menu)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{srv.server_address[1]}/?k={key}'
    browser = app_browser()
    if browser:
        subprocess.Popen([browser, f'--app={url}', '--window-size=520,760', '--user-data-dir=' + os.path.expanduser('~/.scrumdash-window'), '--no-first-run', '--no-default-browser-check'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        webbrowser.open(url)
    while True:   # closing the window ends the menu and the board it started. A reload sends "closed" and comes straight back.
        time.sleep(.25)
        if browser:
            no_maximize()
        now = time.time()
        if (state['closed'] and now - state['closed'] > 4) or now - state['seen'] > 300:
            break
    if state['proc']:
        state['proc'].terminate()
    srv.shutdown()


def main(a):
    carry_over()
    if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if a[:1] == ['hook']:
        return hook()
    if a[:1] == ['join']:
        return join(a[1] if len(a) > 1 else '')
    if a[:1] == ['hold']:
        return hold(hook_input())
    if a[:1] == ['stopcheck']:
        return stopcheck(hook_input())
    if a[:1] == ['track']:
        return track(hook_input())
    if a[:1] == ['nudge']:
        track(hook_input())
        return print(f'Scrumdash: when your work changes, run {PY} "{APP}/tools/claude_step.py" "<what you are doing, in a few plain words>" <0-3>, so the board shows it.')
    if a[:1] == ['setup']:
        try:
            return setup(os.path.abspath(a[1] if len(a) > 1 else '.'))
        except ValueError as e:
            return print(e)
    if not a:
        try:
            return start_menu()
        except Exception as e:   # started without a console: an error must not vanish silently
            try:
                if os.name == 'nt':
                    import ctypes
                    ctypes.windll.user32.MessageBoxW(0, str(e), 'Scrumdash could not start', 0x10)
                    return
                import tkinter as tk
                from tkinter import messagebox
                tk.Tk().withdraw()
                messagebox.showerror('Scrumdash could not start', str(e))
            except Exception:
                raise e
            return
    port, rest = None, []
    i = 0
    while i < len(a):
        if a[i] == '--port':
            port = a[i + 1]; i += 2
        else:
            rest.append(a[i]); i += 1
    project = os.path.abspath(rest[0] if rest else '.')
    prepare(project)
    port = str(port or port_of(project))
    print(f'Scrumdash for {project}: http://127.0.0.1:{port}/ (Ctrl+C stops it)', flush=True)
    if running(port):   # another chat already opened this folder's board: share it and stay alive for this chat's pane
        print('This board is already running.', flush=True)
        while running(port):
            time.sleep(5)
        return
    subprocess.run([sys.executable, os.path.join(APP, 'tools', 'board.py')], cwd=project, env=dict(os.environ, SCRUMDASH_PROJECT=project, SCRUMDASH_PORT=port))


if __name__ == '__main__':
    main(sys.argv[1:])
