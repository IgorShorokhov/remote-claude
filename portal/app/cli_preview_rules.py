import re

# Collapsed tool groups: "Read 1 file", "Running 1 shell command…", "Searched for 2 patterns, read 3 files (ctrl+o to expand)".
_VERB = (r'(?:Read|Reading|Ran|Running|Searched(?:\s+for)?|Searching(?:\s+for)?|Wrote|Writing|Edited|Editing|'
         r'Updated|Updating|Created|Creating|Listed|Listing|Called|Calling|Fetched|Fetching|Found|Finding|'
         r'Loaded|Loading|Used|Using|Did)')
_CLAUSE = _VERB + r'\s+\d+\s+[A-Za-z][\w-]*(?:\s+[A-Za-z][\w-]*)?'
_SUMMARY = re.compile(r'(?i)' + _CLAUSE + r'(?:\s*,\s*(?:and\s+)?' + _CLAUSE + r')*\s*(?:…|\.\.\.)?'
                      r'(?:\s*\((?:ctrl|ctl)[+-]o[^)]*\))?')


def tool_summary(value):
    return bool(_SUMMARY.fullmatch(value.strip()))


# The CLI draws a summarised thought as "● text · summary". It is not part of the answer and never reaches the saved transcript as text.
_THOUGHT = re.compile(r'\s·\s*summary\Z')


def thinking_summary(value):
    return bool(_THOUGHT.search(value.rstrip()))


# While a tool runs the CLI draws a status such as "● Calling claude-in-chrome…", and once calls are grouped "Called claude-in-chrome" or
# "Calling claude-in-chrome 2 times…". None of it is an answer or reaches the saved transcript. A bare "Called <word>" counts only for a
# server-like name (with - . : /), so a sentence such as "Called it." stays text.
_NAME = r'[\w.:/-]+'
_PROGRESS = re.compile(
    r'(?:[●⏺⎿]\s*)?(?:'
    r'(?:Calling|Called)\s+(?:MCP tool:\s*)?' + _NAME + r'\s+\d+\s+times?(?:…|\.\.\.)?'
    r'|Calling\s+(?:MCP tool:\s*)?' + _NAME + r'(?:…|\.\.\.)'
    r'|Called\s+\w+(?:[-.:/]\w+)+'
    r')(?:\s*\(.*\))?\Z')


def progress_line(value):
    return bool(_PROGRESS.match(value.strip()))


# While a tool runs the CLI adds a clock to its description ("… · 13s", "… · 1m 2s") and to the result row ("$ sleep 14 (12s)"),
# plus a hint about the background. The text changes every second, so none of it may become part of a message or a heading.
_CLOCK = re.compile(r'\s·\s+(?:\d+h\s+)?(?:\d+m\s+)?\d+s\Z')
_RESULT_CLOCK = re.compile(r'\s\((?:\d+h\s+)?(?:\d+m\s+)?\d+s\)\Z')
_BACKGROUND = re.compile(r'\(ctrl\+b[^()]*(?:\([^()]*\))?[^()]*\)')


def running_clock(value):
    return bool(_CLOCK.search(value.rstrip()))


def without_clock(value):
    return _RESULT_CLOCK.sub('', _CLOCK.sub('', value.rstrip()))


def background_hint(value):
    return bool(_BACKGROUND.fullmatch(value.strip()))


# "✻ Bloviating… (7s · ↓ 89 tokens)": the CLI is working. A custom status line removes "esc to interrupt" from the footer, so this line says it.
_SPINNER = re.compile(r'[✻✢✶✽✳*·]\s+\S.*…\s*\((?:\d+h\s+)?(?:\d+m\s+)?\d+s\b')


def working_line(value):
    return bool(_SPINNER.match(value.strip()))


def confirmed_tool_heading(value, titles):
    key = ''.join(char.lower() for char in value if char.isalnum())
    return bool(key) and any(key == title or min(len(key),len(title)) >= 12 and (title.startswith(key) or key.startswith(title)) for title in titles)


def tool_heading(value, tools, titles=()):
    if confirmed_tool_heading(value, titles):
        return 'CLI tool', value
    if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_:.-]*', value):
        return 'Pending CLI heading', value
    labels = tools | {'Web Search', 'Web Fetch', 'Fetch', 'Search', 'ToolSearch', 'mcp__'}
    for label in labels:
        if label.startswith(value) or value.startswith(label) and value[len(label):].lstrip().startswith('('):
            return label, value[len(label):].lstrip(' (')
    if value.startswith('mcp__') or re.match(r'^[A-Z][A-Za-z0-9_:.-]{0,79}\s*\(', value):
        return 'CLI tool', value
    if re.match(r'^(?:Read|Searched for|Wrote|Edited|Listed|Ran|Called|Running)\s+\d+\b', value) or tool_summary(value):
        return 'CLI tools', value
    return None


def shell_body(lines):
    return any(re.match(r'^\s*\$\s+\S', line) for line in lines)


def service_line(value):
    return bool(re.match(
        r'^(?:Allowed by (?:auto mode|the auto-mode) classifier\b|'
        r'Did \d+ (?:search|fetch|tool)\b|'
        r'Received [\d.]+\s*(?:bytes?|[KMGT]i?B)\b|'
        r'Fetching(?:\.{3}|…)?$|Searching(?:\.{3}|…)?$|'
        r"You've used \d+(?:\.\d+)?% of your (?:weekly|session) limit\b|"
        r'\d+(?:\.\d+)?% (?:context|tokens?) (?:left|remaining)\b|'
        r'\? for shortcuts\b|'
        r'tmux (?:focus-events off|detected)\b|'
        r'(?:\S\s+)?(?:low|medium|high|xhigh|max)\s*·\s*/effort\b|'
        r'Tool (?:use|execution) (?:rejected|denied)\b)', value, re.I))
