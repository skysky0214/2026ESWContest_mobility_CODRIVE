"""Inference-only UFLDv2 CULane R18 network, matching upstream checkpoint keys.
Architecture derived from cfzd/Ultra-Fast-Lane-Detection-v2 (MIT).
See UFLD-LICENSE for the upstream license.
"""
import torch
from torchvision.models import resnet18

class Backbone(torch.nn.Module):
    def __init__(self):
        super().__init__()
        original=resnet18(weights=None)
        for name in ('conv1','bn1','relu','maxpool','layer1','layer2','layer3','layer4'):
            setattr(self,name,getattr(original,name))
    def forward(self,x):
        for name in ('conv1','bn1','relu','maxpool','layer1','layer2','layer3','layer4'):
            x=getattr(self,name)(x)
        return x

class UfldNetwork(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.model=Backbone()
        self.pool=torch.nn.Conv2d(512,8,1)
        self.cls=torch.nn.Sequential(torch.nn.LayerNorm(4000),torch.nn.Linear(4000,2048),
            torch.nn.ReLU(),torch.nn.Linear(2048,200*72*4+100*81*4+2*72*4+2*81*4))
    def forward(self,x):
        out=self.cls(self.pool(self.model(x)).reshape(-1,4000))
        return (out[:,:57600].reshape(-1,200,72,4),
                out[:,57600:90000].reshape(-1,100,81,4),
                out[:,90000:90576].reshape(-1,2,72,4),
                out[:,90576:].reshape(-1,2,81,4))

def load_network(path):
    model=UfldNetwork()
    checkpoint=torch.load(path,map_location='cpu',weights_only=True)
    weights={k.removeprefix('module.'):v for k,v in checkpoint['model'].items()}
    model.load_state_dict(weights,strict=True,assign=True)
    return model.eval()
