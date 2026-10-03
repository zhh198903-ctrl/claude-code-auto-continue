"""Independent driver for the visible Codex desktop conversation on Windows.

The installed MSIX on this PC is OpenAI.Codex but its window/process is named
ChatGPT. Identify its executable path, not the title or an app-server process.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
from pathlib import PureWindowsPath
import re
import time
from datetime import datetime, timezone

import uiautomation as auto
from codex_cli import Screen, _reset_time

_EMPTY = {'', '随心输入', 'Ask anything', 'Ask Codex anything',
          'Ask Codex to do anything', 'Message Codex', 'Message ChatGPT'}
_STOP = {'停止', 'Stop', 'Stop generating', 'Stop response'}
_EFFORT_LABELS = {'无': 'none', '最低': 'minimal', '轻度': 'low', '低': 'low',
                  '适中': 'medium', '中等': 'medium', '中': 'medium', '标准': 'medium',
                  '高': 'high', '深度': 'high', '极高': 'xhigh',
                  '最高': 'max', '超高': 'ultra'}

def _status_effort(label):
    label = re.split(r'[,，]', label, maxsplit=1)[0].strip()
    for word in sorted(_EFFORT_LABELS, key=len, reverse=True):
        if label.endswith(word):
            return _EFFORT_LABELS[word]
    return label.rsplit(' ', 1)[-1].casefold()

def executable(hwnd):
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.restype = wintypes.HANDLE
    handle = kernel.OpenProcess(0x1000, False, pid.value)
    if not handle:
        return ''
    try:
        path = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(path))
        return path.value if kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)) else ''
    finally:
        kernel.CloseHandle(handle)

def is_desktop_executable(path):
    value = str(path).replace('/', '\\').casefold()
    name = PureWindowsPath(value).name
    return name in {'chatgpt.exe', 'codex.exe'} and (
        '\\windowsapps\\openai.codex_' in value or
        ('\\codex\\' in value and '\\vendor\\' not in value and '\\bin\\' not in value
         and '\\node_modules\\' not in value))

def terminal_handles():
    """Same driver protocol as the CLI; these handles belong only to the app."""
    handles = []
    user = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        name = ctypes.create_unicode_buffer(256)
        user.GetClassNameW(hwnd, name, 256)
        if name.value.startswith('Chrome_WidgetWin') and user.IsWindowVisible(hwnd) and is_desktop_executable(executable(hwnd)):
            handles.append(int(hwnd))
        return True
    if not user.EnumWindows(callback_type(visit), 0):
        raise OSError('Codex desktop window enumeration failed')
    return handles

def window_exists(hwnd):
    return bool(ctypes.windll.user32.IsWindow(wintypes.HWND(hwnd)))

@dataclass
class Window:
    control: object
    hwnd: int
    expected_identity: str = ''
    Name: str = 'Codex App'

def window_from_handle(hwnd):
    if not window_exists(hwnd) or not is_desktop_executable(executable(hwnd)):
        return None
    return Window(auto.ControlFromHandle(int(hwnd)), int(hwnd))

def walk(control, depth=0, count=None):
    count = count if count is not None else [0]
    if depth > 60 or count[0] >= 12000:
        raise RuntimeError('Desktop accessibility tree exceeds inspection bounds')
    count[0] += 1
    yield control
    for child in control.GetChildren():
        yield from walk(child, depth + 1, count)

@dataclass
class View:
    identity: str
    title: str
    screen: Screen
    composer: object = None
    send: object = None
    model_button: object = None
    user_turn: str = ''
    user_text: str = ''

@dataclass
class Submission:
    identity: str
    prompt: str
    user_turn: str
    invoked_at: float | None = None

@dataclass
class SendOutcome:
    status: str
    submission: Submission | None = None

    def __bool__(self):
        return self.status == 'sent'

def _native_error(nodes):
    """A failed turn renders an error after the user entry, outside a reply."""
    users = [i for i, c in enumerate(nodes) if c.ControlTypeName == 'TextControl' and
             c.Name in {'你说：', 'You said:', 'You said：'}]
    if not users:
        return ''
    after = nodes[users[-1] + 1:]
    # Actual local tasks can show commentary/tool output before a failed
    # request. The native error is a focusable outline-none group containing
    # one TextControl, unlike the Paragraph/markdown nodes in message prose.
    native = []
    for i, control in enumerate(after):
        if control.ControlTypeName != 'TextControl' or not re.match(
            r"^(?:你已达到使用上限|You've (?:hit|reached) your usage limit|You have reached|"
            r"Usage limit|超出使用限制|Stream disconnected|stream disconnected|unexpected status|"
            r"Error sending request|error sending request|连接失败|请求失败|无法连接)", control.Name or '', re.I):
            continue
        parent = control.GetParentControl()
        children = parent.GetChildren() if parent is not None else []
        if parent is not None and getattr(parent, 'ClassName', '').strip() == 'outline-none' and len(children) == 1:
            native.append((i, control.Name))
    if native:
        index, error = native[-1]
        # A later assistant entry means this failure already recovered.
        if not any(c.ControlTypeName == 'TextControl' and c.Name in
                   {'ChatGPT 说：', 'ChatGPT said:', 'Assistant said:'} for c in after[index + 1:]):
            return error
        return ''
    actions = [i for i, c in enumerate(after) if c.ControlTypeName == 'ButtonControl' and
               c.Name in {'复制消息', '编辑消息', 'Copy message', 'Edit message'}]
    if not actions:
        return ''
    # The user's submitted text can itself start with an error sentence.
    # Error components appear after the native message action controls.
    after = after[actions[-1] + 1:]
    # A successful/partial assistant reply is ordinary conversation prose.
    # Never schedule from an error sentence quoted in that reply.
    if any(c.ControlTypeName == 'TextControl' and c.Name in
           {'ChatGPT 说：', 'ChatGPT said:', 'Assistant said:'} for c in after):
        return ''
    if any(c.ControlTypeName == 'ButtonControl' and c.Name in
           {'从这里创建聊天分支', 'Branch chat from here', 'Branch conversation from here'} for c in after):
        return ''
    candidates = []
    for c in after:
        if c.ControlTypeName == 'EditControl' or c.Name in {'最新一条回复', 'Latest reply', 'Latest response'}:
            break
        if c.ControlTypeName == 'TextControl' and c.Name.strip() and c.Name not in {'正在思考', 'Thinking'}:
            candidates.append(c.Name)
        if c.ControlTypeName == 'TextControl' and re.match(
            r"^(?:你已达到使用上限|You've (?:hit|reached) your usage limit|You have reached|"
            r"Usage limit|超出使用限制|Stream disconnected|stream disconnected|unexpected status|"
            r"Error sending request|error sending request|连接失败|请求失败|无法连接)", c.Name or '', re.I):
            return c.Name
    return 'native_attention: ' + ' '.join(candidates) if candidates else ''

def read_text(window):
    """Read actual UIA controls, keeping conversation prose out of identity."""
    try:
        document = window.control.DocumentControl(searchDepth=18, AutomationId='RootWebArea')
        if not document.Exists(0, 0):
            return None
        nodes = list(walk(document))
        edits = [c for c in nodes if c.ControlTypeName == 'EditControl' and c.IsEnabled and not c.IsOffscreen]
        # Codex's composer is the last editable control in the document;
        # modal input/search fields must not be mistaken for it.
        composers = [c for c in edits if c.Name in _EMPTY - {''}]
        if len(composers) != 1:
            return View('', document.Name or 'Codex App', Screen(identified=True, blocked=True))
        composer = composers[0]
        scope = composer.GetParentControl()
        for _ in range(2):
            scope = scope.GetParentControl()
        controls = list(walk(scope))
        buttons = [c for c in controls if c.ControlTypeName == 'ButtonControl' and not c.IsOffscreen]
        # Native chat action/composer runtime IDs change when the active
        # conversation changes. Pending recovery never crosses that boundary.
        identity = hashlib.sha256(repr((document.Name, tuple(composer.GetRuntimeId()))).encode()).hexdigest()
        value_pattern = composer.GetValuePattern()
        value = value_pattern.Value if value_pattern else ''
        if not value_pattern:
            pattern = composer.GetTextPattern()
            if pattern:
                value = pattern.DocumentRange.GetText(-1)
            else:
                return None
        draft = (value or '').strip() not in _EMPTY
        busy = any(c.Name in _STOP for c in buttons)
        send = next((c for c in buttons if c.Name in {'发送', '发送消息', 'Send', 'Send message', 'Submit'}), None)
        model = next((c for c in buttons if re.match(r'(?:GPT|gpt|o\d|Codex|自定义|Custom|[5-9](?:\.\d+)?\s+(?:Sol|Astra|Luna|Terra))\b', c.Name or '')
                      or (c.Name or '').startswith('自定义 ')), None)
        # Only native controls trigger error recovery. Assistant text quoting
        # an error, sidebar usage meters, and previous turns do not.
        users = [i for i, c in enumerate(nodes) if c.ControlTypeName == 'TextControl' and
                 c.Name in {'你说：', 'You said:', 'You said：'}]
        turn = nodes[users[-1] + 1:] if users else []
        # Do not pick historical reconnect buttons from earlier turns.
        native_errors = [(i, c.Name) for i, c in enumerate(turn) if c.ControlTypeName == 'ButtonControl'
            and not c.IsOffscreen and re.match(r'^(?:正在重新连接|Reconnecting|重试|Retry|Try again)(?:\s|$)', c.Name or '')]
        if native_errors and any(c.ControlTypeName == 'TextControl' and c.Name in
            {'ChatGPT 说：', 'ChatGPT said:', 'Assistant said:'} for c in turn[native_errors[-1][0] + 1:]):
            native_errors = []
        error = ' | '.join(name for _, name in native_errors)
        message = _native_error(nodes)
        if message:
            error = message
        lower = error.casefold()
        kind = ''
        reset = None
        if error:
            if lower.startswith('native_attention:'):
                kind = 'attention'
            elif re.search(r'使用上限|使用限制|usage limit|usage_limit_reached', lower):
                reset = _reset_time(error, datetime.now(timezone.utc))
                kind = 'quota' if reset else 'quota_retry'
            elif re.search(r'401|403|authentication|unauthorized|登录|身份验证|billing|credits', lower):
                kind = 'attention'
            else:
                kind = 'network'
        blocked = any(not c.IsOffscreen and (c.ControlTypeName in {'MenuControl', 'WindowControl'} or
                      c.Name in {'确认', 'Permission request', '需要批准', 'Approval required'}) for c in nodes)
        mode = any(c.ControlTypeName == 'ButtonControl' and re.search(
            r'(?:当前模式：|current mode:\s*)Codex\b', c.Name or '', re.I) for c in nodes)
        title = document.Name or 'Codex App'
        window.Name = title
        current_label = model.Name if model else ''
        effort_label = current_label.rsplit(' ', 1)[-1]
        effort = _EFFORT_LABELS.get(effort_label, effort_label.casefold() if effort_label.casefold() in
            {'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'} else '')
        current_model = re.sub(r'\s+', '-', current_label.rsplit(' ', 1)[0]).casefold() if effort else ''
        existing_chat = title not in {'ChatGPT', 'Codex', 'New chat', '新聊天'}
        branches = [c for c in nodes if c.ControlTypeName == 'ButtonControl' and c.Name in
                    {'从这里创建聊天分支', 'Branch chat from here', 'Branch conversation from here'}]
        scrolled = any(c.ControlTypeName == 'ButtonControl' and not c.IsOffscreen and c.Name in
                       {'滚动到底部', 'Scroll to bottom'} for c in nodes)
        completion = hashlib.sha256(repr(tuple(branches[-1].GetRuntimeId())).encode()).hexdigest() if branches and not scrolled else ''
        screen = Screen(identified=mode, ready=mode and not blocked and (existing_chat or getattr(window, 'allow_new_chat', False)), running=busy,
                        draft=draft, blocked=blocked,
                        model=current_model, effort=effort,
                        error_kind=kind, reset_utc=reset,
                        error_id=hashlib.sha256((identity + error).encode()).hexdigest() if error else '',
                        completion_id=completion)
        user_turn = ''
        user_text = ''
        if users:
            header = nodes[users[-1]]
            body = None
            for control in nodes[users[-1] + 1:]:
                if control.ControlTypeName == 'ButtonControl' and control.Name in {
                    '复制消息', '编辑消息', 'Copy message', 'Edit message'}:
                    break
                # The native bubble contains the submitted text. Its sibling
                # timestamp is outside it and is not part of the prompt.
                if 'bg-user-message' in getattr(control, 'ClassName', '').split():
                    body = [c.Name for c in walk(control) if c.ControlTypeName == 'TextControl'
                            and not any(x.ControlTypeName == 'TextControl' for x in c.GetChildren())]
                    break
            if body is not None:
                user_text = '\n'.join(body).strip()
                user_turn = hashlib.sha256(repr((tuple(header.GetRuntimeId()), user_text)).encode()).hexdigest()
        return View(identity, title, screen, composer, send, model, user_turn, user_text)
    except Exception:
        return None

def inspect_screen(view, now=None):
    return view.screen if view is not None else Screen()

def session_key(window, view):
    return view.identity

def prepare(window, identity):
    window.expected_identity = identity

def _ready(window):
    view = read_text(window)
    if view is None or not view.identity or (window.expected_identity and view.identity != window.expected_identity):
        return None
    screen = view.screen
    return view if screen.ready and not (screen.running or screen.draft or screen.blocked) else None

def send_prompt(window, prompt, dry_run=False):
    if not prompt.strip() or '\n' in prompt or '\r' in prompt:
        return SendOutcome('held')
    view = _ready(window)
    if view is None:
        return SendOutcome('held')
    if dry_run:
        return SendOutcome('sent')
    user = ctypes.windll.user32
    user.GetForegroundWindow.restype = wintypes.HWND
    previous = user.GetForegroundWindow()
    identity = view.identity
    submission = None
    try:
        window.control.SetActive()
        view.composer.SetFocus()
        time.sleep(.2)
        current = _ready(window)
        if current is None or current.identity != identity or user.GetForegroundWindow() != window.hwnd:
            return SendOutcome('held')
        # ValuePattern updates the contenteditable without typing over a
        # user's draft; the empty value was verified immediately above.
        pattern = current.composer.GetValuePattern()
        if not pattern:
            return SendOutcome('held')
        submission = Submission(identity, prompt, current.user_turn)
        pattern.SetValue(prompt)
        time.sleep(.2)
        return poll_submission(window, submission)
    except Exception:
        return SendOutcome('waiting_send', submission) if submission else SendOutcome('held')
    finally:
        if previous and previous != window.hwnd and user.GetForegroundWindow() == window.hwnd:
            user.SetForegroundWindow(wintypes.HWND(previous))

def _draft(view):
    pattern = view.composer.GetValuePattern() if view.composer else None
    return pattern.Value if pattern else None

def _submitted(view, submission):
    value = _draft(view) if view is not None else None
    return (view is not None and view.identity == submission.identity and
            bool(view.user_turn) and view.user_turn != submission.user_turn and
            view.user_text.strip() == submission.prompt.strip() and
            value is not None and value.strip() in _EMPTY)


def _send_available(button):
    if button is None or not button.IsEnabled or button.IsOffscreen:
        return False
    # Electron can expose IsEnabled=True for an aria-disabled button. The
    # installed native composer also uses these explicit classes for its
    # blocked state; CSS variants such as disabled:opacity-50 are not flags.
    if {'opacity-50', 'cursor-default'} & set(getattr(button, 'ClassName', '').split()):
        return False
    getter = getattr(button, 'GetPropertyValue', None)
    if getter:
        properties = str(getter(auto.PropertyId.AriaPropertiesProperty) or '')
        if re.search(r'(?:^|;)\s*(?:disabled|busy)\s*=\s*true(?:;|$)', properties, re.I):
            return False
    return True

def poll_submission(window, submission, retry_seconds=30, allow_click=True):
    """Wait for native Send availability; account only an observed user turn.

    The receipt owns one exact draft in one visible conversation. No timeout
    or quota estimate grants permission to click a disabled native button.
    """
    user = ctypes.windll.user32
    user.GetForegroundWindow.restype = wintypes.HWND
    previous = user.GetForegroundWindow()
    try:
        view = read_text(window)
        if view is None or not view.identity:
            return SendOutcome('waiting_send', submission)
        if view.identity != submission.identity or (window.expected_identity and view.identity != window.expected_identity):
            return SendOutcome('held')
        if _submitted(view, submission):
            return SendOutcome('sent')
        value = _draft(view)
        if value is None:
            return SendOutcome('waiting_send', submission)
        if value.strip() != submission.prompt.strip():
            # Clearing/editing our draft revokes it. A cleared composer alone
            # is not evidence of submission.
            if submission.invoked_at is not None and value.strip() in _EMPTY and time.monotonic() - submission.invoked_at < 5:
                return SendOutcome('confirming_send', submission)
            return SendOutcome('held')
        if (not allow_click or not view.screen.ready or view.screen.running or view.screen.blocked or
                view.screen.error_kind == 'attention' or not _send_available(view.send)):
            return SendOutcome('waiting_send', submission)
        if submission.invoked_at is not None and time.monotonic() - submission.invoked_at < retry_seconds:
            return SendOutcome('confirming_send', submission)
        window.control.SetActive()
        view.composer.SetFocus()
        time.sleep(.1)
        view = read_text(window)
        if view is None or not view.identity:
            return SendOutcome('waiting_send', submission)
        if _submitted(view, submission):
            return SendOutcome('sent')
        if view.identity != submission.identity:
            return SendOutcome('held')
        value = _draft(view)
        if value is None:
            return SendOutcome('waiting_send', submission)
        if value.strip() != submission.prompt.strip():
            return SendOutcome('held')
        if (user.GetForegroundWindow() != window.hwnd or not view.screen.ready or
                view.screen.running or view.screen.blocked or view.screen.error_kind == 'attention' or
                not _send_available(view.send)):
            return SendOutcome('waiting_send', submission)
        invoke = view.send.GetInvokePattern()
        if not invoke:
            return SendOutcome('waiting_send', submission)
        submission.invoked_at = time.monotonic()
        invoke.Invoke()
        for _ in range(2):
            time.sleep(.15)
            if _submitted(read_text(window), submission):
                return SendOutcome('sent')
        return SendOutcome('confirming_send', submission)
    except Exception:
        return SendOutcome('waiting_send', submission)
    finally:
        if previous and previous != window.hwnd and user.GetForegroundWindow() == window.hwnd:
            user.SetForegroundWindow(wintypes.HWND(previous))

def _model_key(value):
    return re.sub(r'[^a-z0-9]', '', re.sub(r'^gpt[ -]?', '', value.casefold()))

def _act(control):
    if not control.IsEnabled or control.IsOffscreen:
        return False
    invoke = getattr(control, 'GetInvokePattern', lambda: None)()
    expand = getattr(control, 'GetExpandCollapsePattern', lambda: None)()
    select = getattr(control, 'GetSelectionItemPattern', lambda: None)()
    if invoke:
        invoke.Invoke()
    elif expand:
        expand.Expand()
    elif select:
        select.Select()
    else:
        control.Click()
    time.sleep(.15)
    return True

def apply_session_options(window, model, effort):
    view = _ready(window)
    if view is None:
        return False
    if (not model or _model_key(view.screen.model) == _model_key(model)) and (not effort or view.screen.effort == effort):
        return True
    if view.model_button is None:
        return False
    # The default intensity slider can choose a different model. Select an
    # explicit native model before changing effort, preserving the old model
    # when only an effort override was requested.
    if effort and not model:
        model = view.screen.model
        if not model or model in {'自定义', 'custom'}:
            return False
    identity = view.identity
    user = ctypes.windll.user32
    user.GetForegroundWindow.restype = wintypes.HWND
    previous = user.GetForegroundWindow()
    owned_menus = set()

    def menu_nodes():
        current = read_text(window)
        if current is None or current.identity != identity or user.GetForegroundWindow() != window.hwnd:
            return []
        document = window.control.DocumentControl(searchDepth=18, AutomationId='RootWebArea')
        menus = [c for c in walk(document) if c.ControlTypeName == 'MenuControl' and not c.IsOffscreen]
        if len(menus) != 1:
            return []
        if owned_menus and menus[0].AutomationId not in owned_menus:
            return []
        if not owned_menus and menus[0].Name not in {'选择强度', 'Select intensity', view.model_button.Name}:
            return []
        owned_menus.add(menus[0].AutomationId)
        return list(walk(menus[0]))

    def close_menu():
        current = read_text(window)
        if current is None or current.identity != identity or user.GetForegroundWindow() != window.hwnd:
            return
        document = window.control.DocumentControl(searchDepth=18, AutomationId='RootWebArea')
        if any(c.ControlTypeName == 'MenuControl' and c.AutomationId in owned_menus
               for c in walk(document)):
            auto.SendKeys('{Esc}')
            time.sleep(.15)

    try:
        window.control.SetActive()
        if user.GetForegroundWindow() != window.hwnd or not _act(view.model_button):
            return False
        nodes = menu_nodes()
        if not nodes:
            return False
        if model and (_model_key(view.screen.model) != _model_key(model) or effort):
            select_model = next((c for c in nodes if c.ControlTypeName == 'MenuItemControl' and c.Name in {'选择模型', 'Select model'}), None)
            if select_model is None or not _act(select_model):
                return False
            choices = [c for c in menu_nodes() if c.ControlTypeName == 'RadioButtonControl']
            choice = next((c for c in choices if _model_key(c.Name) == _model_key(model)), None)
            if choice is None or not _act(choice):
                return False
            close_menu()
            changed = _ready(window)
            if changed is None or _model_key(changed.screen.model) != _model_key(model):
                return False
            if not effort or changed.screen.effort == effort:
                return True
            if not changed.model_button or not _act(changed.model_button):
                return False
        if effort:
            order = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
            current_effort = view.screen.effort
            directions = ('{Left}', '{Right}') if effort in order and current_effort in order and order.index(effort) < order.index(current_effort) else ('{Right}', '{Left}')
            for direction in directions:
                for _ in range(8):
                    nodes = menu_nodes()
                    status = next((c for c in nodes if c.ControlTypeName == 'StatusBarControl'), None)
                    label = ' '.join(c.Name for c in walk(status) if c.ControlTypeName == 'TextControl') if status else ''
                    if _status_effort(label) == effort:
                        close_menu()
                        result = _ready(window)
                        return bool(result and result.identity == identity and result.screen.effort == effort
                                    and (not model or _model_key(result.screen.model) == _model_key(model)))
                    slider = next((c for c in nodes if c.ControlTypeName == 'MenuItemControl' and c.Name in {'强度', 'Intensity', 'Effort'}), None)
                    if slider is None or not slider.IsEnabled:
                        return False
                    slider.SetFocus()
                    auto.SendKeys(direction)
                    time.sleep(.1)
        close_menu()
        result = _ready(window)
        return bool(result and (not model or _model_key(result.screen.model) == _model_key(model))
                    and (not effort or result.screen.effort == effort))
    except Exception:
        return False
    finally:
        close_menu()
        if previous and previous != window.hwnd and user.GetForegroundWindow() == window.hwnd:
            user.SetForegroundWindow(wintypes.HWND(previous))
