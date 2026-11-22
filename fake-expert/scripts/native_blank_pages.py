"""Conservative, replayable proof for empty physical native PDF pages.

An empty text layer is only a prerequisite. Both a closed set of non-painting
content operators and an exact-white full MediaBox pdftoppm render must agree.
Declared but unused shared resources are irrelevant; every drawing invocation,
annotation, unsupported operation or failed render keeps the ordinary route.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any


PROTOCOL = "native-blank-page-v0.1"
DPI = 150
# These operations alone cannot place marks. In particular, do not allow text
# showing, Do, inline images, shadings, path painting, or external graphics state.
NON_PAINT_OPERATORS = {
    b"q", b"Q", b"cm", b"BT", b"ET", b"Tf", b"TL", b"Tc", b"Tw", b"Tz",
    b"Tr", b"Ts", b"Td", b"TD", b"Tm", b"T*", b"w", b"J", b"j", b"M",
    b"d", b"ri", b"i", b"G", b"g", b"RG", b"rg", b"K", b"k",
}


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _white_ppm(data: bytes) -> tuple[int, int] | None:
    # pdftoppm emits binary RGB P6. Parse its header without treating a pixel
    # byte that happens to be whitespace as part of that header.
    match = re.match(rb"P6\s+(\d+)\s+(\d+)\s+255(?:\r\n|\n| )", data)
    if match is None:
        return None
    width, height = int(match[1]), int(match[2])
    pixels = data[match.end():]
    if width < 1 or height < 1 or len(pixels) != width * height * 3:
        return None
    return (width, height) if pixels.count(255) == len(pixels) else None


def confirm_blank_page(
    page: Any,
    raw_text: str,
    source: Path,
    source_sha256: str,
    physical_page: int,
    renderer: Path | str | None = None,
) -> dict[str, Any] | None:
    """Return a bound proof, or None without changing the nonblank routing.

    No threshold or fuzzy white-pixel rule is used. Raster scans of blank paper,
    white images, invisible marks, short text and annotations remain unconfirmed.
    A missing/broken local renderer also never grants a blank-page exemption.
    """
    if raw_text.strip() or any(page.get(key) for key in ("/Annots", "/AA", "/Group", "/PresSteps")):
        return None
    try:
        from pypdf import __version__ as pypdf_version
        from pypdf.generic import ContentStream

        content = page.get_contents()
        operations = ContentStream(content, getattr(page, "pdf", None)).operations if content is not None else []
        if any(operator not in NON_PAINT_OPERATORS for _operands, operator in operations):
            return None
        # Refuse malformed/unbalanced streams even when their operators do not
        # paint. Parsing/rendering disagreement must not qualify an exemption.
        stack, in_text = 0, False
        for _operands, operator in operations:
            if operator == b"q":
                stack += 1
            elif operator == b"Q":
                stack -= 1
                if stack < 0:
                    return None
            elif operator == b"BT":
                if in_text:
                    return None
                in_text = True
            elif operator == b"ET":
                if not in_text:
                    return None
                in_text = False
        if stack or in_text:
            return None
        media_box = [float(value) for value in page.mediabox]
        if not all(math.isfinite(value) for value in media_box):
            return None
        rotation = int(page.get("/Rotate", 0))
        unit = float(page.get("/UserUnit", 1))
        if rotation not in {0, 90, 180, 270} or unit != 1:
            return None
        width, height = media_box[2] - media_box[0], media_box[3] - media_box[1]
        if rotation in {90, 270}:
            width, height = height, width
        if width <= 0 or height <= 0 or math.ceil(width * DPI / 72) * math.ceil(height * DPI / 72) > 25_000_000:
            return None
        selected = renderer if renderer is not None else shutil.which("pdftoppm")
        if selected is None:
            return None
        executable = Path(selected).expanduser()
        if not executable.is_file():
            found = shutil.which(str(selected))
            if not found:
                return None
            executable = Path(found)
        executable = executable.resolve()
        renderer_hash = _digest(executable)
        version = subprocess.run([str(executable), "-v"], capture_output=True, text=True, timeout=10)
        version_lines = (version.stderr or version.stdout).strip().splitlines()
        if version.returncode or not version_lines:
            return None
        with tempfile.TemporaryDirectory(prefix="tkc-native-blank-") as temporary:
            prefix = Path(temporary) / "full-page"
            rendered = subprocess.run(
                [str(executable), "-f", str(physical_page), "-l", str(physical_page),
                 "-singlefile", "-r", str(DPI), str(source), str(prefix)],
                capture_output=True, timeout=30,
            )
            target = prefix.with_suffix(".ppm")
            if rendered.returncode or rendered.stderr.strip() or not target.is_file():
                return None
            data = target.read_bytes()
        dimensions = _white_ppm(data)
        if dimensions != (math.ceil(width * DPI / 72), math.ceil(height * DPI / 72)):
            return None
        if _digest(source) != source_sha256 or _digest(executable) != renderer_hash:
            return None
        decoded_content = content.get_data() if content is not None else b""
        return {
            "protocol": PROTOCOL,
            "source_sha256": source_sha256,
            "physical_page": physical_page,
            "pypdf_version": pypdf_version,
            "content_sha256": hashlib.sha256(decoded_content).hexdigest(),
            "content_operator_count": len(operations),
            "proof": "non-painting-content-and-exact-white-full-render",
            "render": {
                "renderer": "pdftoppm", "version": version_lines[0],
                "renderer_sha256": renderer_hash, "dpi": DPI, "format": "ppm-rgb8",
                "box": "MediaBox", "media_box_points": media_box,
                "rotation_clockwise": rotation, "pixel_bounds": [0, 0, *dimensions],
                "sha256": hashlib.sha256(data).hexdigest(),
            },
        }
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError):
        return None
