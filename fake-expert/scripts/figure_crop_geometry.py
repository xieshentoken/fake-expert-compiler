"""MediaBox-local figure coordinates to the canonical Poppler raster viewport."""
from __future__ import annotations

import math
from typing import Any, Mapping

PROTOCOL = "figure-crop-mediabox-v0.2"


def pixel_bounds(row: Mapping[str, Any], dpi: int, padding: float = 4.0) -> tuple[int, int, int, int]:
    """Return x, y, width, height for pdftoppm's default MediaBox viewport.

    CropBox metadata never changes the default viewport origin. Intrinsic PDF
    rotation does change it: transform every corner before pixel-edge rounding.
    """
    geometry = row.get("page_geometry") or {}
    width, height = float(row["page_width"]), float(row["page_height"])
    bbox = row["bbox"]
    values = [width, height, padding, *bbox]
    if len(bbox) != 4 or any(isinstance(v, bool) or not isinstance(v, (int,float)) or not math.isfinite(v) for v in values):
        raise ValueError("crop_geometry_invalid")
    if isinstance(dpi, bool) or not isinstance(dpi, int) or dpi < 36 or dpi > 1200 or padding < 0:
        raise ValueError("crop_dpi_or_padding_invalid")
    x0,y0,x1,y1 = bbox
    if width <= 0 or height <= 0 or not 0 <= x0 < x1 <= width or not 0 <= y0 < y1 <= height:
        raise ValueError("crop_bbox_outside_page")
    media = geometry.get("media_box", [0,0,width,height])
    if len(media) != 4 or abs(float(media[2])-float(media[0])-width) > 1e-5 or abs(float(media[3])-float(media[1])-height) > 1e-5:
        raise ValueError("crop_media_geometry_mismatch")
    rotation = geometry.get("rotation", 0)
    if isinstance(rotation,bool) or rotation not in (0,90,180,270):
        raise ValueError("crop_rotation_invalid")
    def transform(x,y):
        if rotation == 90: return height-y,x
        if rotation == 180: return width-x,height-y
        if rotation == 270: return y,width-x
        return x,y
    corners = [transform(x,y) for x in (max(0,x0-padding),min(width,x1+padding))
               for y in (max(0,y0-padding),min(height,y1+padding))]
    scale = dpi/72
    left = math.floor(min(x for x,y in corners)*scale)
    top = math.floor(min(y for x,y in corners)*scale)
    right = math.ceil(max(x for x,y in corners)*scale)
    bottom = math.ceil(max(y for x,y in corners)*scale)
    return left, top, right-left, bottom-top
