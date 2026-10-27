"""Export checkpoint to ONNX and build an FP16 engine on this Jetson."""
import argparse
from pathlib import Path
import torch
import tensorrt as trt
from codrive.sensor_input.external.lane.ufld_network import load_network
p=argparse.ArgumentParser();p.add_argument('--onnx-only',action='store_true');a=p.parse_args()
root=Path('artifacts/models/vision/ufld')
root.mkdir(parents=True, exist_ok=True)
onnx=root/'culane_res18.onnx'
if not onnx.exists():
    net=load_network(root/'culane_res18.pth').half()
    with torch.inference_mode():
        torch.onnx.export(net,torch.zeros((1,3,320,1600),dtype=torch.float16),str(onnx),
            input_names=['input'],output_names=['loc_row','loc_col','exist_row','exist_col'],
            opset_version=17,dynamo=False)
    del net
    print('ONNX exported',flush=True)
if a.onnx_only:raise SystemExit(0)
logger=trt.Logger(trt.Logger.INFO)
builder=trt.Builder(logger)
network=builder.create_network(0)
parser=trt.OnnxParser(network,logger)
assert parser.parse_from_file(str(onnx)), '\n'.join(str(parser.get_error(i)) for i in range(parser.num_errors))
config=builder.create_builder_config()
config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE,256<<20)
config.set_flag(trt.BuilderFlag.FP16)
config.builder_optimization_level=1
engine=builder.build_serialized_network(network,config)
assert engine is not None
(root/'culane_res18.engine').write_bytes(engine)
print('FP16 engine saved',flush=True)
