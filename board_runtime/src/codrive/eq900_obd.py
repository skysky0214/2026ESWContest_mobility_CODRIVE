"""Headless EQ900 fixed diagnostic reads via CANable SLCAN (not listen-only).

Signal formulas adapted from skskelwl/eq900-canable-monitor @
fa477535338e82ba7f17a6e1847a3d10acaaec47 and OBDb/Genesis contributors,
CC-BY-SA-4.0; license/attribution: config/can/eq900-reference/.
No ECU controls, arbitrary requests, retries, or functional broadcasts.
"""
import queue,threading,time
from .models import Header,VehicleState

QUERIES=((0x7D4,0x7DC,bytes.fromhex('2101')),
         (0x7A0,0x7A8,bytes.fromhex('22B008')),
         (0x7E1,0x7E9,bytes.fromhex('2201A0')))
FC=bytes.fromhex('3000000000000000')
def packet(command):return bytes([len(command)])+command+bytes(7-len(command))
ALLOWED={(tx,packet(cmd)) for tx,_,cmd in QUERIES}

class ReadGuard:
    def __init__(self):self.last=None;self.flow_target=None
    def check(self,ident,data,now,*,extended=False,remote=False,fd=False):
        if extended or remote or fd:raise ValueError('Only fixed classic 11-bit diagnostic reads are allowed')
        if data==FC and ident==self.flow_target:
            self.flow_target=None;return
        if (ident,data) not in ALLOWED:raise ValueError('Unapproved diagnostic request')
        if self.last is not None and now-self.last<.1-1e-9:raise ValueError('Maximum total rate is 10 requests/s')
        self.last=now

class Transaction:
    def __init__(self):
        self.item=None;self.part=None;self.index=0;self.next_request=0.
        self.timeouts=0;self.sequence_errors=0;self.negative_responses=0
    def request(self,now):
        if self.item and now>self.item['deadline']:
            self.item=self.part=None;self.timeouts+=1
        if self.item or now<self.next_request:return None
        tx,rx,cmd=QUERIES[self.index%3];self.index+=1
        self.item=dict(tx=tx,rx=rx,cmd=cmd,deadline=now+.4,sent_at=now,sent_wall=time.time_ns())
        self.part=None;self.next_request=now+.1
        return tx,packet(cmd)
    def receive(self,rx,data,now):
        item=self.item
        if not item or rx!=item['rx'] or not data or now>item['deadline']:return None,None
        positive=bytes([item['cmd'][0]+0x40])+item['cmd'][1:]
        kind=data[0]>>4;payload=None;flow=None
        if kind==0:
            n=data[0]&15
            if not 1<=n<=7 or len(data)<n+1:return None,None
            candidate=data[1:n+1]
            if candidate.startswith(positive):payload=candidate
            elif candidate[:2]==bytes([0x7f,item['cmd'][0]]):
                self.negative_responses+=1;self.item=self.part=None
        elif kind==1 and len(data)==8 and self.part is None:
            total=((data[0]&15)<<8)|data[1]
            if 8<=total<=256 and data[2:].startswith(positive):
                self.part=dict(total=total,data=bytearray(data[2:]),seq=1)
                flow=(item['tx'],FC)
        elif kind==2 and self.part:
            part=self.part
            if (data[0]&15)!=(part['seq']&15):
                self.sequence_errors+=1;self.item=self.part=None;return None,None
            part['seq']+=1;part['data'].extend(data[1:])
            if len(part['data'])>=part['total']:payload=bytes(part['data'][:part['total']])
        if payload is not None:
            result=(rx,payload,item['sent_wall']);self.item=self.part=None
            return result,None
        return None,flow

def decode(rx,payload):
    if rx==0x7DC and payload.startswith(bytes.fromhex('6101')) and len(payload)>=13:
        return {'steering_wheel_deg':int.from_bytes(payload[11:13],'big',signed=True)/10}
    if rx==0x7A8 and payload.startswith(bytes.fromhex('62B008')) and len(payload)>=10:
        return {'brake_pressed':bool(payload[9]&0x10)}
    if rx==0x7E9 and payload.startswith(bytes.fromhex('6201A0')) and len(payload)>=12:
        return {'speed_mps':payload[9]/3.6,'accelerator_pct':payload[11]*100/255}
    return {}

class Eq900ObdWorker:
    def __init__(self,config_path):
        self.path=config_path;self.stop=threading.Event();self.lock=threading.Lock()
        self.latest=queue.Queue(maxsize=1);self.thread=None
        self.state={'status':'waiting_device','mode':'fixed_obd_reads','receive_only':False,'max_requests_s':10}
    def status(self):
        with self.lock:return dict(self.state)
    def update(self,**values):
        with self.lock:self.state.update(values)
    def start(self):
        import yaml
        self.config=yaml.safe_load(open(self.path))
        if self.config.get('protocol')!='eq900_fixed_obd' or self.config.get('bitrate')!=500000:
            self.update(status='invalid_configuration',error='Expected verified EQ900 fixed read profile');return
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()
    def take_latest(self):
        try:return self.latest.get_nowait()
        except queue.Empty:return None
    def run(self):
        while not self.stop.is_set():
            try:
                import can
                from can.interfaces.slcan import slcanBus
                from serial.tools import list_ports
                ports=[p.device for p in list_ports.comports() if p.vid==0x16D0 and p.pid==0x117E]
                if len(ports)!=1:raise RuntimeError('Connect exactly one CANable 16d0:117e to Pi')
                guard=ReadGuard()
                class FixedBus(slcanBus):
                    def open(bus):bus._write('A0');bus._write('O')
                    def send(bus,msg,timeout=None):
                        guard.check(msg.arbitration_id,bytes(msg.data),time.monotonic(),extended=msg.is_extended_id,remote=msg.is_remote_frame,fd=msg.is_fd)
                        return super().send(msg,timeout)
                with FixedBus(channel=ports[0],bitrate=500000,tty_baudrate=115200,listen_only=False) as bus:
                    self.update(status='querying',port=ports[0],error=None)
                    tx=Transaction();values=dict(speed_mps=0.,steering_wheel_deg=0.,brake_pressed=False,accelerator_pct=0.);times={};sequence=0
                    while not self.stop.is_set():
                        now=time.monotonic();request=tx.request(now)
                        if request:
                            guard.flow_target=None
                            bus.send(can.Message(arbitration_id=request[0],data=request[1],is_extended_id=False),timeout=.2)
                            tx.next_request=time.monotonic()+.1
                        msg=bus.recv(timeout=.005)
                        if msg is None:
                            if tx.timeouts:self.update(timeouts=tx.timeouts)
                            continue
                        if msg.is_extended_id or msg.is_error_frame or msg.is_remote_frame or msg.is_fd:continue
                        result,flow=tx.receive(msg.arbitration_id,bytes(msg.data),time.monotonic())
                        if flow:
                            guard.flow_target=flow[0]
                            bus.send(can.Message(arbitration_id=flow[0],data=flow[1],is_extended_id=False),timeout=.2)
                        if result:
                            rx,payload,stamp=result;updates=decode(rx,payload)
                            if not updates:continue
                            values.update(updates);times.update({k:stamp for k in updates});sequence+=1
                            header=Header.now('pi5.eq900_obd',sequence)
                            sample=VehicleState(header,**values,can_valid=0<=header.capture_time_ns-times.get('speed_mps',0)<=750_000_000,signal_times_ns=tuple(sorted(times.items())))
                            try:self.latest.get_nowait()
                            except queue.Empty:pass
                            self.latest.put_nowait(sample)
                            self.update(status='receiving',values=values.copy(),can_valid=sample.can_valid,source=header.source_id,responses=sequence,last_response_ns=header.capture_time_ns,signal_times_ns=times.copy(),timeouts=tx.timeouts,sequence_errors=tx.sequence_errors)
            except Exception as exc:
                self.update(status='waiting_device',error=str(exc)[:240])
                self.stop.wait(2)
    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=2)
