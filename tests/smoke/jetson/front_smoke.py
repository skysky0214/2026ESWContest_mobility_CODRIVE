"""Run real YOLO inference and CODRIVE publishing on sampled driving frames."""
from pathlib import Path
from collections import Counter
import json
import cv2
import torch
from codrive.sensor_input.external.front_perception import UltralyticsFrontPerception, YoloFrontPublisher
from codrive.transport import InMemoryBus
from codrive.contracts.models import to_json

root = Path(__file__).resolve().parent
out = root / 'test-results'
out.mkdir(parents=True,exist_ok=True)
cap = cv2.VideoCapture(str(root / 'test_driving.mp4'))
assert cap.isOpened(), 'Input video could not be opened'
assert torch.cuda.is_available(), 'CUDA is unavailable'
perception = UltralyticsFrontPerception(root / 'yolo11n.pt', device=0)
counts = Counter()
records = []
try:
    with InMemoryBus() as bus:
        publisher = YoloFrontPublisher(bus=bus, perception=perception)
        for i in (0, 120, 240, 360, 480):
            cap.set(cv2.CAP_PROP_POS_FRAMES, i)
            ok, frame = cap.read()
            assert ok, f'Cannot read frame {i}'
            frame = cv2.resize(frame, (1280, 720))
            result = publisher.publish_frame(frame)
            records.append(json.loads(to_json(result)))
            counts.update(obj.class_name for obj in result.objects)
            print(f'frame={i}, objects={len(result.objects)}, inference_ms={result.inference_ms:.1f}', flush=True)
finally:
    cap.release()
(out / 'front-perception.json').write_text(json.dumps(records, ensure_ascii=False, indent=2))
print('Detected classes:', dict(counts))
assert sum(counts.values()) > 0, 'No objects detected in sampled video frames'
print('PASS: CUDA + video decode + YOLO + CODRIVE in-memory publishing')
