import json
from urllib.request import urlopen

import pytest

pytest.importorskip("cyclonedds")

from codrive.transport.mjpeg import MjpegStreamConfig, MjpegStreamServer


def test_mjpeg_health_and_latest_frame_stream() -> None:
    server = MjpegStreamServer(MjpegStreamConfig(host="127.0.0.1", port=0))
    server.start()
    try:
        server.publish_jpeg(b"\xff\xd8codrive\xff\xd9")
        with urlopen(f"http://127.0.0.1:{server.port}/health", timeout=2) as response:
            health = json.loads(response.read())
        assert health["status"] == "ok"
        assert health["sequence"] == 1
        assert health["stream"] == "/stream.mjpg"

        with urlopen(server.stream_url, timeout=2) as response:
            assert response.headers.get_content_type() == "multipart/x-mixed-replace"
            assert response.readline() == b"--frame\r\n"
            assert response.readline() == b"Content-Type: image/jpeg\r\n"
            length_header = response.readline().decode().strip()
            assert length_header == "Content-Length: 11"
            assert response.readline() == b"\r\n"
            assert response.read(11) == b"\xff\xd8codrive\xff\xd9"
    finally:
        server.close()


@pytest.mark.parametrize("quality", [19, 96])
def test_mjpeg_config_rejects_invalid_quality(quality: int) -> None:
    with pytest.raises(ValueError, match="jpeg_quality"):
        MjpegStreamConfig(jpeg_quality=quality)
