"""Post-measurement clock offset interval over an existing SSH control channel.

Interval arithmetic uses local send <= remote timestamp <= local receive.
This diagnostic is not a hardware PTP guarantee during an earlier measurement.
"""
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time
from run_session import HOSTS


def sample(host):
    address,control,_=HOSTS[host]
    code="import sys,time;print('ready',flush=True)\nfor line in sys.stdin:print(time.time_ns(),flush=True)"
    command=['ssh','-o','BatchMode=yes','-o','ControlPath='+(control or 'none'),address,
             'python3 -u -c '+shlex.quote(code)]
    p=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True,bufsize=1)
    assert p.stdout.readline().strip()=='ready'
    rows=[]
    for _ in range(15):
        before=time.time_ns();p.stdin.write('x\n');p.stdin.flush();remote=int(p.stdout.readline());after=time.time_ns()
        rows.append({'local_send_ns':before,'remote_ns':remote,'local_receive_ns':after,
                     'rtt_ns':after-before,'offset_low_ns':remote-after,'offset_high_ns':remote-before})
    p.stdin.close();p.wait(timeout=5)
    best=min(rows,key=lambda r:r['rtt_ns'])
    return {'best':best,'probes':rows,'offset_estimate_ns':(best['offset_low_ns']+best['offset_high_ns'])/2,
            'half_rtt_uncertainty_ns':best['rtt_ns']/2}


if __name__=='__main__':
    path=Path(sys.argv[1]);path.write_text(json.dumps({h:sample(h) for h in HOSTS},indent=2)+'\n')
