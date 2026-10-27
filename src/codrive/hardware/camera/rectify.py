"""Rectify measured lens distortion before inference and all preview outputs."""
import json
from pathlib import Path

import cv2
import numpy as np


class CameraRectifier:
    def __init__(self, calibration_path: Path, size=(1280, 720), zoom=1.0):
        data = json.loads(calibration_path.read_text())
        if data.get('schema_version') != 1 or data.get('model') not in {'pinhole', 'fisheye'}:
            raise ValueError('Unsupported camera calibration schema/model')
        original = np.asarray(data['image_size'], dtype=float)
        matrix = np.asarray(data['camera_matrix'], dtype=float)
        distortion = np.asarray(data['distortion_coefficients'], dtype=float)
        if original.shape != (2,) or not np.isfinite(original).all() or np.any(original <= 0):
            raise ValueError('Invalid calibration image size')
        if matrix.shape != (3, 3) or not np.isfinite(matrix).all():
            raise ValueError('Invalid camera matrix')
        if matrix[0, 0] <= 0 or matrix[1, 1] <= 0 or not np.allclose(matrix[2], [0, 0, 1]):
            raise ValueError('Invalid camera projection')
        allowed = (4,) if data['model'] == 'fisheye' else (4, 5, 8, 12, 14)
        if distortion.ndim != 1 or distortion.size not in allowed or not np.isfinite(distortion).all():
            raise ValueError('Invalid distortion coefficients')
        if not np.isfinite(zoom) or not 1 <= zoom <= 3:
            raise ValueError('Camera zoom must be between 1 and 3')
        scale = np.asarray(size, dtype=float)/original
        if not np.isclose(scale[0], scale[1], rtol=1e-3):
            raise ValueError('Calibration and frame must have the same aspect ratio')
        matrix = matrix.copy()
        matrix[0] *= scale[0]
        matrix[1] *= scale[1]
        self.size = tuple(size)
        self.model = data['model']
        if self.model == 'fisheye':
            new_matrix = cv2.fisheye.estimateNewCameraMatrixForUndistortRectify(
                matrix, distortion, self.size, np.eye(3), balance=0, new_size=self.size)
        else:
            new_matrix, _ = cv2.getOptimalNewCameraMatrix(
                matrix, distortion, self.size, 0, self.size, centerPrincipalPoint=True)
        new_matrix[0, 0] *= zoom
        new_matrix[1, 1] *= zoom
        if not np.isfinite(new_matrix).all() or min(new_matrix[0, 0], new_matrix[1, 1]) <= 0:
            raise ValueError('Invalid rectified camera projection')
        self.camera_matrix = new_matrix
        if self.model == 'fisheye':
            self.maps = cv2.fisheye.initUndistortRectifyMap(
                matrix, distortion, np.eye(3), new_matrix, self.size, cv2.CV_16SC2)
        else:
            self.maps = cv2.initUndistortRectifyMap(
                matrix, distortion, None, new_matrix, self.size, cv2.CV_16SC2)
        mask = cv2.remap(np.full((size[1], size[0]), 255, np.uint8), *self.maps, cv2.INTER_LINEAR)
        self.valid_fraction = float(np.mean(mask == 255))
        if self.valid_fraction < .98:
            raise ValueError(f'Rectification has too much empty border ({self.valid_fraction:.1%} valid); increase zoom')

    def apply(self, frame):
        if (frame.shape[1], frame.shape[0]) != self.size:
            raise ValueError('Frame dimensions changed after camera calibration')
        return cv2.remap(frame, *self.maps, cv2.INTER_LINEAR)
