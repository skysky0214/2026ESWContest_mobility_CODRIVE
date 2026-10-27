"""Bounded physical-camera measurement; remote log collection only after windows.

No screenshots, API polling, synthetic inputs, driving commands or GPU fences.
"""
import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import time

ROOT=Path(__file__).resolve().parents[2]
HOSTS={
 'front':('one@192.168.98.254','/tmp/codrive-j1-lane-20261027', [('codrive-front-camera','front')]),
 'cabin':('two@192.168.96.162','/tmp/codrive-j2-latency-20261027',[('codrive-dms','driver'),('codrive-audio','audio')]),
 'pi':('rp@192.168.99.21',None,[('codrive-pi','pi')])}


def ssh(host, command):
    address,control,_=HOSTS[host]
    args=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=5','-o','ControlPath='+ (control or 'none')]
    return subprocess.check_output(args+[address,command],text=True,timeout=60)


def all_hosts(fn):
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        return dict(zip(HOSTS,pool.map(fn,HOSTS)))


def configure(host, enabled, session):
    _,_,units=HOSTS[host]
    cmds=[]
    for unit,label in units:
        path=f'$HOME/.config/systemd/user/{unit}.service.d/99-latency-measure.conf'
        text='[Service]\n'
        if enabled:
            text+=f'Environment=CODRIVE_LATENCY_FILE=/tmp/{session}-{label}.jsonl\nEnvironment=CODRIVE_LATENCY_SESSION={session}\nEnvironment=CODRIVE_LATENCY_EVERY={10 if label=="pi" else 1}\n'
        else:text+='UnsetEnvironment=CODRIVE_LATENCY_FILE CODRIVE_LATENCY_SESSION CODRIVE_LATENCY_EVERY\n'
        cmds += [f'mkdir -p "$HOME/.config/systemd/user/{unit}.service.d"',
                 f'printf %s {shlex.quote(text)} > "{path}"']
    cmds+=['systemctl --user daemon-reload', 'systemctl --user restart '+' '.join(u for u,_ in units)]
    return ssh(host,' && '.join(cmds))


def probe(host):
    _,_,units=HOSTS[host]
    if host=='pi':units=units+[('codrive-hmi-display','browser')]
    # Read only at window boundaries, never in the hot path.
    code='''import json,os,time
units=%r
result={'time_ns':time.time_ns(),'monotonic_ns':time.monotonic_ns(),'units':{}}
for unit,label in units:
 import subprocess
 pid=int(subprocess.check_output(['systemctl','--user','show',unit,'-p','MainPID','--value']))
 if pid:
  raw=open('/proc/%%d/stat'%%pid).read().rsplit(')',1)[1].split()
  result['units'][label]={'pid':pid,'cpu_ticks':int(raw[11])+int(raw[12]),'rss_pages':int(raw[21])}
for unit,label in units:
 cg=subprocess.check_output(['systemctl','--user','show',unit,'-p','ControlGroup','--value'],text=True).strip()
 base='/sys/fs/cgroup'+cg
 try:
  cpu=dict(line.split() for line in open(base+'/cpu.stat'))
  result['units'].setdefault(label,{})['cgroup_cpu_usec']=int(cpu['usage_usec'])
  result['units'][label]['cgroup_memory_bytes']=int(open(base+'/memory.current').read())
 except (OSError,KeyError):pass
result['clock_ticks']=os.sysconf('SC_CLK_TCK')
print(json.dumps(result))
''' % units
    return json.loads(ssh(host,'python3 -c '+shlex.quote(code)))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--session',default=time.strftime('physical-%Y%m%d-%H%M%S'))
    p.add_argument('--window',type=int,default=30)
    p.add_argument('--main-duration',type=int,default=120)
    a=p.parse_args();out=ROOT/'artifacts/latency'/a.session;out.mkdir(parents=True,exist_ok=False)
    config={'session':a.session,'window_s':a.window,'main_duration_s':a.main_duration,
            'scope':'physical frame application receipt; software result rendering; no physical exposure/display/sound timestamps',
            'debug_overlay':False,'carla_pixel_relay':False,'gpu_fences_added':False,
            'external_ground_calibration':'not valid for current camera placement; metric TTC unavailable',
            'hmi_video':'production clean stream retained','phase_order':['off1','on1','off2','on2','main']}
    (out/'session.json').write_text(json.dumps(config,indent=2)+'\n')
    # Refuse to call a run clean while the old 10 Hz Pi recorder remains active.
    check=subprocess.run(['systemctl','--user','is-active','--quiet','codrive-carla-scenarios'])
    if check.returncode==0:
        control=subprocess.check_output(['systemctl','--user','show','codrive-carla-scenarios','-p','ExecStart','--value'],text=True)
        if '--no-pi-recorder' not in control:raise RuntimeError('Stop the scenario recorder before a clean latency run')
    with socket.socket() as connection:
        connection.settimeout(.2)
        if connection.connect_ex(('127.0.0.1',18082))==0:
            raise RuntimeError('CARLA pixel relay is still running')
    audit=all_hosts(lambda h:ssh(h,'systemctl --user show '+ ' '.join(u for u,_ in HOSTS[h][2])+' -p ExecStart'))
    if '--stream-url' in audit['front'] or '--debug-stream-port' in audit['front'] or '--log ' in audit['front']:
        raise RuntimeError('Front input/debug/log configuration is not clean')
    if '--diagnostics' in audit['cabin'] or '--preview' in audit['cabin']:
        raise RuntimeError('Cabin diagnostics/preview must be disabled')
    (out/'clean-audit.json').write_text(json.dumps(audit,indent=2)+'\n')
    preflight=all_hosts(lambda h:ssh(h,'hostname; chronyc tracking; systemctl --user is-active '+ ' '.join(u for u,_ in HOSTS[h][2])))
    (out/'preflight.json').write_text(json.dumps(preflight,indent=2)+'\n')
    phases=[]
    try:
        for phase in ('off1','on1','off2','on2','main'):
            enabled=not phase.startswith('off')
            # Same restart/warmup procedure on both sides of A/B.
            print('CONFIGURE',phase,flush=True)
            all_hosts(lambda h:configure(h,enabled,a.session))
            time.sleep(15)
            before=all_hosts(probe)
            start=time.time_ns()
            print('MEASURE',phase,'seconds',a.main_duration if phase=='main' else a.window,flush=True)
            time.sleep(a.main_duration if phase=='main' else a.window)
            after=all_hosts(probe)
            phases.append({'phase':phase,'start_ns':start,'end_ns':time.time_ns(),'before':before,'after':after})
            (out/'phases.json').write_text(json.dumps(phases,indent=2)+'\n')
            print('DONE',phase,flush=True)
    finally:
        time.sleep(2)  # Drain the bounded logger outside the measurement window.
        # Leave production running without tracing and flush pending asynchronous records.
        all_hosts(lambda h:configure(h,False,a.session))
        for host,(_,_,units) in HOSTS.items():
            for _,label in units:
                try:data=ssh(host,'cat '+shlex.quote('/tmp/'+a.session+'-'+label+'.jsonl'))
                except subprocess.CalledProcessError:data=''
                (out/(label+'.jsonl')).write_text(data)
        (out/'postflight.json').write_text(json.dumps(all_hosts(lambda h:ssh(h,'chronyc tracking')),indent=2)+'\n')
    print('RESULTS',out,flush=True)


if __name__=='__main__':main()
