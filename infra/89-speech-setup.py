import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request

ROOT = Path('/opt/agent-speech')
UNIT = '''[Unit]
Description=Local speech recognition for Agent Portal
After=network.target

[Service]
User=claude
Group=agent-portal
UMask=0007
RuntimeDirectory=agent-speech
RuntimeDirectoryMode=0750
ExecStart=/opt/agent-speech/venv/bin/python -I /opt/agent-speech/worker.py
Environment=OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=1 TOKENIZERS_PARALLELISM=false HF_HUB_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
RestrictAddressFamilies=AF_UNIX
CapabilityBoundingSet=
MemoryMax=1400M
CPUQuota=200%
TasksMax=32

[Install]
WantedBy=multi-user.target
'''


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def run(args, **kwargs):
    subprocess.run(args, check=True, **kwargs)


def health():
    connection = http.client.HTTPConnection('localhost', timeout=10)
    connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.sock.settimeout(10)
    connection.sock.connect('/run/agent-speech/speech.sock')
    try:
        connection.request('GET', '/health')
        response = connection.getresponse()
        return response.status == 200 and json.loads(response.read())['available']
    finally:
        connection.close()


def install(source):
    if subprocess.check_output(['/usr/bin/python3','-c','import sys;print("%d.%d" % sys.version_info[:2])'],text=True).strip() != '3.12':
        raise RuntimeError('Speech lock requires VPS Python 3.12')
    ROOT.mkdir(mode=0o755, exist_ok=True)
    lock = source / 'speech' / 'requirements.lock'
    version = sha(lock)[:16]
    venv = ROOT / ('venv-' + version)
    marker = venv / '.complete'
    if not marker.exists():
        run(['/usr/bin/python3','-m','venv',str(venv)])
        run([str(venv/'bin/pip'),'install','--disable-pip-version-check','--require-hashes','--only-binary=:all:','-r',str(lock)])
        run([str(venv/'bin/pip'),'check'])
        marker.write_text(version + '\n')
    link = ROOT / 'venv'
    if not link.is_symlink() or link.resolve() != venv:
        temp = ROOT / '.venv-next'
        temp.unlink(missing_ok=True)
        temp.symlink_to(venv.name)
        temp.replace(link)
    manifest = json.loads((source/'speech/model.json').read_text())
    models = ROOT / 'model'
    models.mkdir(mode=0o755, exist_ok=True)
    for item in manifest['assets']:
        name = item['name']
        if name not in ('model.bin','config.json','tokenizer.json','vocabulary.txt'):
            raise RuntimeError('Unexpected model asset')
        path = models / name
        if path.is_file() and sha(path) == item['sha256']:
            continue
        print('Downloading speech asset ' + name, flush=True)
        temp = models / (name + '.part')
        try:
            with urllib.request.urlopen(item['url'], timeout=120) as response, temp.open('wb') as stream:
                shutil.copyfileobj(response, stream, length=1024*1024)
        except Exception:
            raise RuntimeError('Cannot download speech asset ' + name) from None
        if sha(temp) != item['sha256']:
            raise RuntimeError('Speech asset hash differs: ' + name)
        os.chmod(temp, 0o644)
        temp.replace(path)
    changed = False
    for source_file, target in ((source/'speech/worker.py',ROOT/'worker.py'),(lock,ROOT/'requirements.lock'),(source/'speech/model.json',ROOT/'model.json')):
        if not target.exists() or sha(source_file) != sha(target):
            shutil.copyfile(source_file,target);os.chmod(target,0o644);changed=True
    unit = Path('/etc/systemd/system/agent-speech.service')
    if not unit.exists() or unit.read_text() != UNIT:
        unit.write_text(UNIT);os.chmod(unit,0o644);changed=True
        run(['systemctl','daemon-reload'])
    run(['systemctl','enable','agent-speech.service'])
    active = subprocess.run(['systemctl','is-active','--quiet','agent-speech.service']).returncode == 0
    if changed or not active:
        run(['systemctl','restart','agent-speech.service'])
    config = Path('/etc/caddy/Caddyfile')
    text = config.read_text()
    before = 'Permissions-Policy "camera=(), microphone=(), geolocation=()"'
    after = 'Permissions-Policy "camera=(), microphone=(self), geolocation=()"'
    if before in text:
        if text.count(before) != 1:
            raise RuntimeError('Ambiguous microphone policy')
        funnel = json.loads(subprocess.check_output(['tailscale','funnel','status','--json'],text=True))
        host = next(iter(funnel['Web'])).removesuffix(':443')
        fd, name = tempfile.mkstemp(prefix='.speech-',dir=config.parent)
        temp = Path(name)
        try:
            with os.fdopen(fd,'w') as stream:stream.write(text.replace(before,after))
            info = config.stat();os.chmod(temp,info.st_mode);os.chown(temp,info.st_uid,info.st_gid)
            run(['caddy','validate','--config',str(temp),'--adapter','caddyfile'],env={**os.environ,'PORTAL_HOST':host},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            temp.replace(config)
            run(['systemctl','reload','caddy'])
        finally:
            temp.unlink(missing_ok=True)
    elif after not in text:
        raise RuntimeError('Microphone policy not found')
    for _ in range(50):
        try:
            if health():return
        except (OSError, ValueError):pass
        time.sleep(.2)
    raise RuntimeError('Speech service did not become ready')


def check(source):
    checks = {'service':subprocess.run(['systemctl','is-active','--quiet','agent-speech']).returncode == 0,
              'worker_matches_source':sha(ROOT/'worker.py') == sha(source/'speech/worker.py'),
              'requirements_match_source':sha(ROOT/'requirements.lock') == sha(source/'speech/requirements.lock'),
              'microphone_self':'microphone=(self)' in Path('/etc/caddy/Caddyfile').read_text(),
              'socket_healthy':health()}
    manifest = json.loads((source/'speech/model.json').read_text())
    checks['model_hashes'] = all(sha(ROOT/'model'/item['name']) == item['sha256'] for item in manifest['assets'])
    print(json.dumps({'checks':checks},indent=2))
    if not all(checks.values()):raise RuntimeError('Speech installation check failed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode',choices=('deploy','check'))
    parser.add_argument('source',type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:raise SystemExit('Run on VPS as root')
    if args.mode == 'deploy':install(args.source)
    check(args.source)
