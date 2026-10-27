import json
from pathlib import Path
import shlex
import subprocess
import time

ADDRESS='one@192.168.98.254'
CONTROL='/tmp/codrive-j1-lane-20261027'
REMOTE='/home/one/ws/ESW2026-main/build/perception-eval-current'


def ssh(code):
    return subprocess.check_output(['ssh','-o','BatchMode=yes','-o','ControlPath='+CONTROL,
        ADDRESS,'python3 -c '+shlex.quote(code)],text=True,timeout=30)


def capture(ident, destination=None):
    code=f'''import json,time
from pathlib import Path
root=Path({REMOTE!r}); ident={ident!r}
request=root/'request.json';temp=root/'request.tmp'
temp.write_text(json.dumps({{'id':ident}}));temp.replace(request)
for _ in range(120):
 if (root/(ident+'.ready')).exists():
  print((root/(ident+'.json')).read_text());break
 time.sleep(.1)
else:raise RuntimeError('Capture worker did not acknowledge request')
'''
    metadata=json.loads(ssh(code))
    if destination:
        destination=Path(destination);destination.parent.mkdir(parents=True,exist_ok=True)
        subprocess.run(['scp','-q','-o','ControlPath='+CONTROL,ADDRESS+':'+REMOTE+'/'+ident+'.png',str(destination)],check=True)
    return metadata
