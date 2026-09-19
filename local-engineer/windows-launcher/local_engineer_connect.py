"""Interactive user launcher: passwords stay in SSH's terminal, never in this script."""
import base64
import json
import pathlib
import shlex
import subprocess
import sys

HOST='macmini@100.125.201.25'
LOCAL_SOCKET=str(pathlib.Path.home()/'.ssh/local-engineer-control')
REMOTE_SOCKET='/Users/macmini/.ssh/local-engineer-wsl-control'
SSH=['ssh','-S',LOCAL_SOCKET,'-o','ConnectTimeout=10']

def run(args,quiet=False):
    return subprocess.run(args,stdout=subprocess.DEVNULL if quiet else None,
                          stderr=subprocess.DEVNULL if quiet else None).returncode

def main():
    request=json.loads(base64.b64decode(sys.argv[1]).decode('utf-8'))
    if request.get('mode') not in ('fix','inspect'): raise ValueError('invalid mode')
    if run(SSH+['-O','check',HOST],True):
        print('Authenticate to Mac mini in this terminal.',flush=True)
        code=run(['ssh','-M','-S',LOCAL_SOCKET,'-o','ControlPersist=3600',
                  '-o','ServerAliveInterval=30','-o','ServerAliveCountMax=3',
                  '-o','StrictHostKeyChecking=accept-new','-o','ConnectTimeout=10','-fN',HOST])
        if code: return code
    remote=['ssh','-S',REMOTE_SOCKET,'-o','ConnectTimeout=10','-p','2222','stc@127.0.0.1']
    check=['ssh','-S',REMOTE_SOCKET,'-O','check','-p','2222','stc@127.0.0.1']
    if run(SSH+['-o','BatchMode=yes',HOST,shlex.join(check)],True):
        # If this forward already exists, SSH reports a nonzero code. Authentication
        # below still checks the destination; never cancel an existing connection.
        run(SSH+['-O','forward','-R','127.0.0.1:2222:127.0.0.1:22',HOST],True)
        print('Authenticate to WSL as stc in this terminal.',flush=True)
        connect=['ssh','-M','-S',REMOTE_SOCKET,'-o','ControlPersist=3600',
                 '-o','StrictHostKeyChecking=accept-new','-o','ConnectTimeout=10',
                 '-p','2222','-fN','stc@127.0.0.1']
        code=run(SSH+['-t',HOST,shlex.join(connect)])
        if code: return code
    if run(SSH+['-o','BatchMode=yes',HOST,shlex.join(remote+['true'])]):
        print('WSL connection unavailable; no project changes made.',file=sys.stderr)
        return 2
    if request.get('resume'):
        command=['/Users/macmini/bin/local-engineer','resume',request['resume']]
    else:
        command=['/Users/macmini/bin/local-engineer',request['mode'],request['project'],request['task']]
    return run(SSH+['-o','BatchMode=yes',HOST,shlex.join(command)])

if __name__=='__main__': raise SystemExit(main())
