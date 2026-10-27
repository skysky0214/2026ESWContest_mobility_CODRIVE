"""Bounded, non-dropping hardware file decode for Jetson benchmarks."""
from pathlib import Path
import numpy as np
import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gst, GstVideo
Gst.init(None)

class JetsonVideoCapture:
    def __init__(self, path=None, *, pipeline=None):
        self.pipeline = Gst.parse_launch(pipeline or
            'filesrc name=source ! qtdemux ! h264parse ! nvv4l2decoder ! '
            'nvvidconv ! video/x-raw,format=BGRx,width=1280,height=720 ! '
            'videoconvert ! video/x-raw,format=BGR ! '
            'appsink name=frames sync=false max-buffers=1 drop=false')
        if pipeline is None:
            self.pipeline.get_by_name('source').set_property('location', str(Path(path).resolve()))
        self.last_pts_ns = None
        self.sink = self.pipeline.get_by_name('frames')
        self.bus = self.pipeline.get_bus()
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.release()
            raise RuntimeError('Hardware decoder failed to start')
    def isOpened(self):
        return self.pipeline is not None
    def read(self):
        sample = self.sink.emit('try-pull-sample', 10 * Gst.SECOND)
        if sample is None:
            error = self.bus.pop_filtered(Gst.MessageType.ERROR)
            if error:
                message, debug = error.parse_error()
                raise RuntimeError(f'{message}: {debug}')
            if self.sink.get_property('eos'):
                return False, None
            raise TimeoutError('Hardware decoder produced no frame for 10 seconds')
        info = GstVideo.VideoInfo.new_from_caps(sample.get_caps())
        buffer = sample.get_buffer()
        self.last_pts_ns = None if buffer.pts == Gst.CLOCK_TIME_NONE else int(buffer.pts)
        ok, mapped = buffer.map(Gst.MapFlags.READ)
        if not ok:
            raise RuntimeError('Could not map decoded video buffer')
        try:
            frame = np.ndarray((info.height, info.width, 3), dtype=np.uint8,
                               buffer=mapped.data, offset=info.offset[0],
                               strides=(info.stride[0], 3, 1)).copy()
        finally:
            buffer.unmap(mapped)
        return True, frame
    def release(self):
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
