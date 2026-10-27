from __future__ import annotations

from typing import ClassVar

from codrive.sensor_input.external.front_perception import (
    UltralyticsFrontPerception,
    YoloFrontPublisher,
    normalized_bbox,
)
from codrive.contracts.models import TrafficLight
from codrive.transport.model_bus import decode_model
from codrive.transport import InMemoryBus
from codrive.contracts.models import ExternalPerception


class _Frame:
    shape = (100, 200, 3)

    def __getitem__(self, _key: object) -> _Frame:
        return self

    size = 1


class _Tensor:
    def __init__(self, value: object) -> None:
        self.value = value

    def cpu(self) -> _Tensor:
        return self

    def tolist(self) -> object:
        return self.value


class _Boxes:
    cls = _Tensor([0, 2])
    conf = _Tensor([0.9, 0.8])
    xyxy = _Tensor([[20, 10, 100, 90], [110, 10, 180, 90]])


class _Result:
    names: ClassVar[dict[int, str]] = {0: "person", 2: "car"}
    boxes: ClassVar[_Boxes] = _Boxes()


class _Model:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}

    def predict(self, _frame: object, **_kwargs: object) -> list[object]:
        self.kwargs = _kwargs
        return [_Result()]


def test_normalized_bbox_uses_frame_dimensions() -> None:
    assert normalized_bbox((20, 10, 100, 90), _Frame()).x2 == 0.5


def test_front_publisher_maps_yolo_objects_and_publishes(monkeypatch: object) -> None:
    module = __import__("codrive.sensor_input.external.front_perception", fromlist=["*"])
    monkeypatch.setattr(module, "traffic_light_from_bgr", lambda *_: (TrafficLight.UNKNOWN, 0.0))
    monkeypatch.setattr(module, "lane_confidence_from_bgr", lambda *_: 0.5)
    bus = InMemoryBus(clock_ns=lambda: 1_000_000_000)
    model = _Model()
    publisher = YoloFrontPublisher(bus, UltralyticsFrontPerception("yolo11n.pt", model=model))
    output = publisher.publish_frame(_Frame(), capture_time_ns=1_000_000_000)
    wire = bus.take_json("external_perception")[0]
    assert [item.class_name for item in output.objects] == ["pedestrian", "car"]
    assert model.kwargs["classes"] == [0, 1, 2, 3, 5, 7, 9, 11]
    assert output.lane_confidence == 0.5
    assert decode_model(wire, ExternalPerception) == output


def test_refined_uncertain_signal_never_reaches_publication(monkeypatch):
    module = __import__('codrive.sensor_input.external.front_perception',fromlist=['*'])
    monkeypatch.setattr(module,'lane_confidence_from_bgr',lambda _:0.)
    class Refine:
        minimum=.4
        config={'minimum_state_evidence':.65}
        def crop_detections(self,*_):return ()
        def filter(self,items):return items
        def state(self,*_):return TrafficLight.RED,.4
    class Model:
        def predict(self,*_,**__):
            class Boxes:
                cls=_Tensor([9,2]);conf=_Tensor([.9,.8]);xyxy=_Tensor([[20,10,35,50],[80,30,180,90]])
            class Result:
                names={9:'traffic light',2:'car'};boxes=Boxes()
            return [Result()]
    bus=InMemoryBus();publisher=YoloFrontPublisher(bus,UltralyticsFrontPerception('test',model=Model(),refinement=Refine()))
    for _ in range(3):output=publisher.publish_frame(_Frame())
    assert output.traffic_light==TrafficLight.UNKNOWN
    assert [o.class_name for o in output.objects]==['car']


def test_ground_geometry_authority_skips_unused_image_lane_calculation(monkeypatch):
    from dataclasses import replace
    module=__import__('codrive.sensor_input.external.front_perception',fromlist=['*'])
    def unused(*_):raise AssertionError('Ground geometry replaces this result; avoid duplicate Hough work')
    monkeypatch.setattr(module,'lane_confidence_from_bgr',unused)
    class Geometry:
        def enrich(self,result,lanes):return replace(result,lane_confidence=.75)
    publisher=YoloFrontPublisher(InMemoryBus(),UltralyticsFrontPerception('test',model=_Model(),estimate_image_lane=False),geometry=Geometry())
    assert publisher.publish_frame(_Frame()).lane_confidence==.75
