"""Classic SocketCAN receiver. No CAN data transmission API; requires listen-only."""
import json,socket,struct,subprocess
from .panda_can import CanFrame


class SocketCanReceiver:
    def __init__(self, interface):
        info=json.loads(subprocess.run(['ip','-details','-json','link','show','dev',interface],check=True,capture_output=True,text=True,timeout=3).stdout)[0]
        data=info.get('linkinfo',{}).get('info_data',{})
        modes=data.get('ctrlmode',[])
        if not any(str(mode).upper().replace('_','-')=='LISTEN-ONLY' for mode in modes):
            raise RuntimeError('SocketCAN must be configured listen-only before opening')
        self.socket=socket.socket(socket.PF_CAN,socket.SOCK_RAW,socket.CAN_RAW)
        self.socket.bind((interface,));self.socket.setblocking(False)
    def __enter__(self):return self
    def __exit__(self,*args):self.socket.close()
    def read_available(self):
        frames=[]
        for _ in range(256):
            try:data=self.socket.recv(16)
            except BlockingIOError:break
            if len(data)!=16:continue
            ident,length,payload=struct.unpack('=IB3x8s',data)
            if ident & (0x40000000|0x20000000) or length>8:continue
            frames.append(CanFrame(ident&0x1fffffff,payload[:length],0))
        return frames
