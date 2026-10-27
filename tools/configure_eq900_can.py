"""Configure an already identified SocketCAN adapter, never transmit CAN data."""
import argparse,json,subprocess,yaml
p=argparse.ArgumentParser();p.add_argument('--config',default='config/can/eq900.yaml');p.add_argument('--apply',action='store_true');a=p.parse_args()
c=yaml.safe_load(open(a.config));rate=c.get('bitrate');channel=c.get('channel','can0')
if c.get('receive_only') is not True or type(rate) is not int or rate not in (10000,20000,50000,100000,125000,250000,500000,800000,1000000):raise SystemExit('Set bitrate from the EQ900 reference first; no guessed bitrate is applied')
commands=[['ip','link','set','dev',channel,'down'],['ip','link','set','dev',channel,'type','can','bitrate',str(rate),'listen-only','on'],['ip','link','set','dev',channel,'up']]
print(json.dumps(commands))
if a.apply:
 for cmd in commands:subprocess.run(cmd,check=True)
 from codrive.hardware.socketcan import SocketCanReceiver
 with SocketCanReceiver(channel):pass
 print('Listen-only verified; no CAN data transmitted')
