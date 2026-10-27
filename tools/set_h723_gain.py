"""Set volatile PCM gain using the installed MIC2 firmware control protocol."""
import argparse,time
import serial
from codrive.config import load_config
from codrive.hardware.h723_audio import Mic2Decoder
p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--gain',type=int,required=True);a=p.parse_args()
if not 1<=a.gain<=256:p.error('gain must be 1..256')
cfg=load_config(a.config);port=cfg['audio']['h723']['device'];payload=b'GAIN'+a.gain.to_bytes(2,'little');crc=0
for b in payload:
 crc^=b
 for _ in range(8):crc=((crc<<1)^(7 if crc&128 else 0))&255
with serial.Serial(port,3000000,timeout=.1) as stream:
 stream.write(payload+bytes([crc,10]));stream.flush();decoder=Mic2Decoder();deadline=time.monotonic()+3
 while time.monotonic()<deadline:
  frames=decoder.feed(stream.read(16384))
  if any(f.gain==a.gain for f in frames):print('H723 microphone gain acknowledged:',a.gain);break
 else:raise RuntimeError('MIC2 did not acknowledge gain')
