"""Local, fixed-scale PNG template matching. Never maps bitmap positions to a display."""

from __future__ import annotations

import base64
import binascii
import struct
from typing import cast

import cv2
import numpy as np
from numpy.typing import NDArray

from pulse_runtime.drivers.android.agent_transport import ConnectionFault
from pulse_runtime.perception.observation import _png

MAX_TEMPLATE_PIXELS = 262_144
MAX_TEMPLATE_BYTES = 1_048_576
MAX_IMAGE_MATCHES = 128


class ImageMatchError(ValueError):
    pass


def decode_png(value: str, *, template: bool = False) -> NDArray[np.uint8]:
    try:
        if len(value) > 5_592_408:
            raise ImageMatchError("image_resource_limit")
        encoded = base64.b64decode(value, validate=True)
        if len(encoded) < 33 or encoded[:8] != b"\x89PNG\r\n\x1a\n" or encoded[12:16] != b"IHDR":
            raise ImageMatchError("invalid_image")
        width, height = struct.unpack_from(">II", encoded, 16)
        if width == 0 or height == 0 or width * height > 1_500_000:
            raise ImageMatchError("invalid_image")
        if template and (width * height > MAX_TEMPLATE_PIXELS or len(encoded) > MAX_TEMPLATE_BYTES):
            raise ImageMatchError("image_resource_limit")
        _png(value, width, height)
        decoded = cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        if decoded is None or decoded.shape[:2] != (height, width) or decoded.ndim != 3:
            raise ImageMatchError("invalid_image")
        if decoded.shape[2] == 4:
            if not np.all(decoded[:, :, 3] == 255):
                raise ImageMatchError("image_transparency_unsupported")
            decoded = decoded[:, :, :3]
        if decoded.shape[2] != 3:
            raise ImageMatchError("invalid_image")
        return cast(NDArray[np.uint8], np.ascontiguousarray(decoded))
    except (ValueError, binascii.Error, ConnectionFault, cv2.error) as exc:
        code = str(exc) if isinstance(exc, ImageMatchError) else "invalid_image"
        raise ImageMatchError(code) from None


def match_image(
    image: NDArray[np.uint8], template: NDArray[np.uint8], threshold: float
) -> list[tuple[dict[str, int], float]]:
    height, width = template.shape[:2]
    if height > image.shape[0] or width > image.shape[1]:
        return []
    try:
        # Normalizing by the maximum pixel error also handles flat/black templates. The usual
        # correlation coefficient cannot distinguish occurrences of a constant template.
        errors = cv2.matchTemplate(image, template, cv2.TM_SQDIFF)
        scores = np.clip(1.0 - errors / (template.size * 255.0**2), 0.0, 1.0)
        peaks = cv2.dilate(scores, np.ones((3, 3), dtype=np.uint8))
        rows, columns = np.nonzero((scores >= threshold) & (scores >= peaks))
        if len(rows) > MAX_IMAGE_MATCHES:
            raise ImageMatchError("image_match_limit")
        # Keep every qualifying local maximum, including ties. Picking the best score or
        # suppressing overlapping occurrences must not manufacture a unique target.
        return [
            (
                {"left": int(x), "top": int(y), "right": int(x) + width, "bottom": int(y) + height},
                float(scores[y, x]),
            )
            for y, x in zip(rows, columns, strict=True)
        ]
    except cv2.error:
        raise ImageMatchError("image_match_failed") from None
