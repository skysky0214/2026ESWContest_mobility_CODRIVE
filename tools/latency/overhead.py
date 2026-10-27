"""A/B cgroup CPU and queue-drop observations, without claiming causal precision."""
import argparse
import json
from pathlib import Path
from collections import defaultdict
import statistics


def main():
 p=argparse.ArgumentParser();p.add_argument('directory',type=Path);a=p.parse_args()
 phases=json.loads((a.directory/'phases.json').read_text());windows=[]
 for phase in phases:
  if phase['phase']=='main':continue
  for host,before in phase['before'].items():
   after=phase['after'][host];seconds=(after['monotonic_ns']-before['monotonic_ns'])/1e9
   for label,b in before['units'].items():
    c=after['units'].get(label,{})
    if b.get('pid')!=c.get('pid'):continue
    if 'cgroup_cpu_usec' not in b or 'cgroup_cpu_usec' not in c:continue
    cpu_seconds=(c['cgroup_cpu_usec']-b['cgroup_cpu_usec'])/1e6
    counter_scope='service_cgroup'
    if label=='browser' and cpu_seconds==0 and c.get('cpu_ticks',0)>b.get('cpu_ticks',0):
      # Snap moves Firefox outside the launcher's cgroup, but MainPID is tracked.
      cpu_seconds=(c['cpu_ticks']-b['cpu_ticks'])/before['clock_ticks']
      counter_scope='firefox_parent_process_only'
    windows.append({'phase':phase['phase'],'host':host,'unit':label,'seconds':seconds,
      'cpu_one_core_pct':cpu_seconds/seconds*100,'counter_scope':counter_scope,
      'memory_bytes':c.get('cgroup_memory_bytes') if counter_scope=='service_cgroup' else None})
 summary=[];unavailable=[]
 for unit in sorted({r['unit'] for r in windows}):
  off=[r['cpu_one_core_pct'] for r in windows if r['unit']==unit and r['phase'].startswith('off')]
  on=[r['cpu_one_core_pct'] for r in windows if r['unit']==unit and r['phase'].startswith('on')]
  if unit=='browser' and off and on and not any(off+on):
   unavailable.append({'unit':'browser','reason':'Launcher service cgroup does not contain active Snap Firefox processes; zero launcher CPU is not browser CPU'})
   continue
  if off and on:summary.append({'unit':unit,'off_cpu_pct':statistics.mean(off),'on_cpu_pct':statistics.mean(on),
        'difference_percentage_points':statistics.mean(on)-statistics.mean(off),
        'off_windows':off,'on_windows':on,'counter_scope':sorted({r['counter_scope'] for r in windows if r['unit']==unit})})
 dropped={}
 for path in a.directory.glob('*.jsonl'):
  maximum=0
  for line in path.open():
   try:maximum=max(maximum,json.loads(line).get('dropped_records',0))
   except ValueError:pass
  dropped[path.stem]=maximum
 report={'windows':windows,'summary':summary,'unavailable_units':unavailable,'max_dropped_measurement_records':dropped,
  'limitations':['CPU use is a resource-overhead observation, not proof of a latency change',
    'Snap Firefox parent-process CPU is observed from /proc; renderer and GPU child processes are not included',
    'Two short repeated A/B windows remain sensitive to scene and system variation',
    'No extra GPU synchronization, screenshots or API polling is used inside measurement windows',
    'The production HMI clean video and active perception workloads remain enabled']}
 (a.directory/'overhead.json').write_text(json.dumps(report,indent=2)+'\n')
 print(json.dumps(summary,indent=2))


if __name__=='__main__':main()
