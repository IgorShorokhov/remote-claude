"""One API Agent turn in the background (decision 227): the model streams, calls tools, sees their results, and goes on until it answers.

The request that sends a message only starts the turn. The page reads the reply as it is being written from live() and the saved steps
from the chat's events, so a reload or a phone going to sleep does not stop the work. Stop ends the stream and kills the running command.
A portal restart does end a turn; the transcript then shows it as interrupted.
"""
import json
import os
import threading
import time

import httpx

from . import api_agent_tools as tools

MAX_STEPS = 100
HISTORY_CHARS = 400_000
OLD_RESULT_CHARS = 2_000
KEEP_WHOLE_STEPS = 6
THINKING_CHARS = 20_000
RETRY_WAITS = (2, 6)
EFFORTS = ('low', 'medium', 'high')

RUNS: dict[str, 'Run'] = {}
RUNS_LOCK = threading.Lock()


class ProviderError(Exception):
    def __init__(self, text, retry=False):
        super().__init__(text)
        self.retry = retry


class Run:
    def __init__(self, sid, turn_id):
        self.sid, self.turn_id = sid, turn_id
        self.cancel = threading.Event()
        self.step = 0
        self.text = ''
        self.state = 'thinking'
        self.tool = ''
        self.started = time.time()
        self.workspace = None
        self.thread = None

    def live(self):
        return {'running': self.thread is not None and self.thread.is_alive(), 'turn_id': self.turn_id, 'step': self.step,
                'state': self.state, 'text': self.text, 'tool': self.tool, 'started': self.started}


def running(sid):
    run = RUNS.get(sid)
    return run is not None and run.thread is not None and run.thread.is_alive()


def stop(sid):
    run = RUNS.get(sid)
    if run is None or not running(sid):
        return False
    run.cancel.set()
    if run.workspace:
        run.workspace.kill()
    return True


def history(rows):
    """OpenAI messages from the saved events: each model step with its tool calls, then their results. Old results are shortened
    and the oldest exchanges dropped so the history stays under HISTORY_CHARS."""
    messages, step, last = [], None, None
    for row in rows:
        kind, extra = row['kind'], row['extra']
        if kind == 'user':
            messages.append({'role': 'user', 'content': row['text']}); step = None
        elif kind in ('assistant', 'tool') and extra.get('step') is not None:
            key = (extra['step'], extra.get('turn'))
            if step is None or key != last:
                step = {'role': 'assistant', 'content': None}
                messages.append(step); last = key
            if kind == 'assistant':
                step['content'] = ((step['content'] or '') + row['text']).strip() or None
            else:
                step.setdefault('tool_calls', []).append({'id': extra['id'], 'type': 'function',
                                                          'function': {'name': extra['name'], 'arguments': extra.get('raw') or '{}'}})
        elif kind == 'assistant':
            messages.append({'role': 'assistant', 'content': row['text']}); step = None
        elif kind == 'tool_result' and extra.get('tool_id'):
            messages.append({'role': 'tool', 'tool_call_id': extra['tool_id'], 'content': row['text']})
    messages = paired(messages)
    return fit(messages)


def paired(messages):
    """Every tool call needs its result and every result its call; an interrupted step loses the half that is missing."""
    answered = {m['tool_call_id'] for m in messages if m['role'] == 'tool'}
    asked = set()
    out = []
    for m in messages:
        if m['role'] == 'assistant' and m.get('tool_calls'):
            calls = [c for c in m['tool_calls'] if c['id'] in answered]
            asked.update(c['id'] for c in calls)
            m = {**m, 'tool_calls': calls} if calls else {k: v for k, v in m.items() if k != 'tool_calls'}
            if not m.get('content') and not m.get('tool_calls'):
                continue
        if m['role'] == 'tool' and m['tool_call_id'] not in asked:
            continue
        out.append(m)
    return out


def size(messages):
    return sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)


def fit(messages):
    tool_at = [i for i, m in enumerate(messages) if m['role'] == 'tool']
    for i in tool_at[:-KEEP_WHOLE_STEPS] if len(tool_at) > KEEP_WHOLE_STEPS else []:
        if size(messages) <= HISTORY_CHARS:
            break
        text = messages[i]['content']
        if len(text) > OLD_RESULT_CHARS:
            messages[i] = {**messages[i], 'content': text[:OLD_RESULT_CHARS] + '\n… (older output shortened)'}
    while size(messages) > HISTORY_CHARS:
        starts = [i for i, m in enumerate(messages) if m['role'] == 'user']
        if len(starts) < 2:
            break
        messages = messages[starts[1]:]
    return messages


def stream(post, payload, run):
    """One model step. Returns (text, reasoning, tool_calls, usage). Raises ProviderError; retry=True only before any output arrived."""
    text, reasoning, calls, usage, got = '', '', {}, None, False
    with post(payload) as response:
        code = response.status_code
        if code == 400:
            raise ProviderError('The provider refused the request: ' + response.read()[:300].decode('utf-8', 'replace'))
        if code != 200:
            raise ProviderError(f'The provider answered HTTP {code}.', retry=code in (408, 409, 429) or code >= 500)
        if 'event-stream' not in response.headers.get('content-type', ''):
            return whole(response.read(), run)
        for line in response.iter_lines():
            if run.cancel.is_set():
                raise tools.Stopped()
            if not line.startswith('data:'):
                continue
            data = line[5:].strip()
            if data == '[DONE]':
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            if chunk.get('error'):
                raise ProviderError('The provider stopped the reply: ' + str(chunk['error'])[:300], retry=not got)
            usage = chunk.get('usage') or usage
            for choice in chunk.get('choices') or []:
                delta = choice.get('delta') or {}
                if delta.get('reasoning_content') or delta.get('reasoning'):
                    reasoning += delta.get('reasoning_content') or delta.get('reasoning') or ''
                    got = True
                if delta.get('content'):
                    text += delta['content']; got = True
                    run.text, run.state = text, 'writing'
                for call in delta.get('tool_calls') or []:
                    got = True
                    slot = calls.setdefault(call.get('index', 0), {'id': '', 'name': '', 'arguments': ''})
                    slot['id'] = call.get('id') or slot['id']
                    fn = call.get('function') or {}
                    slot['name'] += fn.get('name') or ''
                    slot['arguments'] += fn.get('arguments') or ''
                    run.state, run.tool = 'tool', slot['name']
    ordered = [calls[i] for i in sorted(calls)]
    for n, call in enumerate(ordered):
        call['id'] = call['id'] or f'call_{run.step}_{n}'
    return text, reasoning, ordered, usage


def whole(body, run):
    """A provider that ignores stream=true answers with one JSON completion."""
    try:
        data = json.loads(body)
        message = data['choices'][0]['message']
    except (ValueError, KeyError, IndexError, TypeError):
        raise ProviderError('The provider sent a reply in an unknown format.') from None
    calls = [{'id': c.get('id') or f'call_{run.step}_{n}', 'name': (c.get('function') or {}).get('name') or '',
              'arguments': (c.get('function') or {}).get('arguments') or '{}'} for n, c in enumerate(message.get('tool_calls') or [])]
    for c in calls:
        if not isinstance(c['arguments'], str):
            c['arguments'] = json.dumps(c['arguments'])
    text = message.get('content') if isinstance(message.get('content'), str) else ''
    run.text = text
    return text, message.get('reasoning_content') or '', calls, data.get('usage')


def start(sid, turn_id, request, save, prompt, root, ask):
    """request: the turn's settings (model, effort, messages built from history). save(kind, text, extra) stores an event.
    Returns the Run; refuses (None) when this chat already has one."""
    with RUNS_LOCK:
        if running(sid):
            return None
        run = Run(sid, turn_id)
        RUNS[sid] = run
    run.thread = threading.Thread(target=work, args=(run, request, save, prompt, root, ask), name=f'api-agent-{sid[:8]}', daemon=True)
    run.thread.start()
    return run


def work(run, request, save, prompt, root, ask):
    try:
        ws = tools.Workspace(root, run.cancel)
    except OSError as error:
        save('assistant', f'API Agent could not open the chat folder ({error.strerror or error}). Nothing was run.', {'final': True, 'error': True})
        return
    run.workspace = ws
    turn = run.turn_id or str(time.time())
    messages = [{'role': 'system', 'content': prompt}] + request['messages']
    images = request.get('images') or []
    if images and messages[-1]['role'] == 'user':
        messages[-1] = {'role': 'user', 'content': [{'type': 'text', 'text': messages[-1]['content']}] +
                        [{'type': 'image_url', 'image_url': {'url': url}} for url in images]}
    effort = request.get('effort') if request.get('effort') in EFFORTS else None
    try:
        while run.step < MAX_STEPS:
            run.step += 1
            run.text, run.state, run.tool = '', 'thinking', ''
            payload = {'model': request['model'], 'messages': messages, 'tools': tools.TOOLS, 'stream': True,
                       'stream_options': {'include_usage': True}}
            if effort:
                payload['reasoning_effort'] = effort
            text, reasoning, calls, usage = step_with_retry(run, request['post'], payload)
            marks = {'step': run.step, 'turn': turn}
            if reasoning.strip():
                save('thinking', reasoning.strip()[:THINKING_CHARS], {})
            if usage:
                save('usage', '', {'usage': usage, 'model': request['model'], **marks})
            if text.strip():
                save('assistant', text, {**marks, 'final': not calls})
            if not calls:
                if not text.strip():
                    save('assistant', 'The model ended its turn without an answer.', {'final': True})
                return
            messages.append({'role': 'assistant', 'content': text or None,
                             'tool_calls': [{'id': c['id'], 'type': 'function', 'function': {'name': c['name'], 'arguments': c['arguments'] or '{}'}}
                                            for c in calls]})
            shown = []
            for call in calls:
                try:
                    args = json.loads(call['arguments'] or '{}')
                except ValueError:
                    args = {}
                save('tool', '', {**marks, 'id': call['id'], 'name': call['name'], 'raw': call['arguments'] or '{}',
                                  'args': tools.summary(args) if isinstance(args, dict) else ''})
                run.state, run.tool = 'tool', call['name']
                try:
                    result = tools.run(ws, call['name'], call['arguments'], ask)
                except tools.Stopped:
                    save('tool_result', 'Stopped by the user.', {'tool_id': call['id'], 'error': True})
                    raise
                save('tool_result', result.text, {'tool_id': call['id'], 'error': result.error})
                messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': result.text})
                if result.image:
                    shown.append(result.image)
                if run.cancel.is_set():
                    raise tools.Stopped()
            if shown:
                messages.append({'role': 'user', 'content': [{'type': 'text', 'text': 'Pictures from read_file:'}] +
                                 [{'type': 'image_url', 'image_url': {'url': img['url']}} for img in shown]})
        save('assistant', f'Stopped after {MAX_STEPS} steps. Send a message to continue.', {'final': True})
    except tools.Stopped:
        if run.text.strip():
            save('assistant', run.text, {'step': run.step, 'turn': turn})
        save('interrupted', '', {})
    except ProviderError as error:
        if run.text.strip():
            save('assistant', run.text, {'step': run.step, 'turn': turn})
        save('assistant', f'{error} Nothing was repeated; send the message again to retry.', {'final': True, 'error': True})
    except Exception as error:  # the turn must always end with a visible line, never a silent stop
        save('assistant', f'API Agent stopped on an internal error ({type(error).__name__}). Nothing was repeated.', {'final': True, 'error': True})
    finally:
        run.state, run.text, run.tool = 'idle', '', ''
        run.workspace = None


def step_with_retry(run, post, payload):
    for attempt, wait in enumerate(RETRY_WAITS + (None,)):
        try:
            return stream(post, payload, run)
        except (httpx.HTTPError, ProviderError) as error:
            retry = isinstance(error, httpx.HTTPError) or error.retry
            if isinstance(error, ProviderError) and 'refused' in str(error) and 'reasoning_effort' in payload:
                payload = {k: v for k, v in payload.items() if k != 'reasoning_effort'}  # a model without effort levels
                continue
            if not retry or wait is None or run.text:
                raise error if isinstance(error, ProviderError) else ProviderError('The connection to the provider broke.') from None
            run.state = 'retrying'
            if run.cancel.wait(wait):
                raise tools.Stopped()
    raise ProviderError('The provider did not answer.')
