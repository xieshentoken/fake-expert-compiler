"""Conservative, replayable proof for empty physical native PDF pages.

An empty text layer is only a prerequisite. Restricted nonpainting content or
opaque Device-white rectangles must agree with an exact-white full MediaBox
render. Unused shared resources are irrelevant; images, strokes, annotations,
unsupported operations and failed renders never qualify by their pixels alone.
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
from compiler_version import NATIVE_BLANK_PAGE_PROTOCOL, NATIVE_PAGE_DIAGNOSTIC_SCHEMA, NATIVE_WHITE_VECTOR_BLANK_PROTOCOL


PROTOCOL = NATIVE_BLANK_PAGE_PROTOCOL
DIAGNOSTIC_SCHEMA = NATIVE_PAGE_DIAGNOSTIC_SCHEMA
WHITE_VECTOR_PROTOCOL = NATIVE_WHITE_VECTOR_BLANK_PROTOCOL
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


def _content_diagnostic(page: Any, raw_text: str) -> tuple[str, str, int]:
    """Prove only nonpainting streams or finite opaque Device-white rectangles.

    This examines every operation, so black marks subsequently covered in white
    never qualify. No image, text-showing, clipping, opacity or unknown operator
    is allowed. Graphic-state restore reinstates the previous (default black)
    fill and transform; paths themselves are not part of the q/Q state stack.
    """
    from pypdf.generic import ContentStream

    if raw_text.strip():
        return "content", "native_text_present", 0
    for key, reason in (("/Annots", "page_annotations_present"), ("/AA", "page_actions_present"),
                        ("/Group", "transparency_group_present"), ("/PresSteps", "unsupported_page_feature")):
        if page.get(key):
            return "review_required", reason, 0
    content = page.get_contents()
    operations = ContentStream(content, getattr(page, "pdf", None)).operations if content is not None else []
    fill_white, matrix, path, rectangles, in_text = False, (1., 0., 0., 1., 0., 0.), [], 0, False
    saved: list[tuple[bool, tuple[float, ...]]] = []
    box = [float(value) for value in page.mediabox]
    for operands, operator in operations:
        if operator in {b"Tj", b"TJ", b"'", b'"'}:
            return "content", "text_showing_present", 0
        if operator in {b"Do", b"INLINE IMAGE"}:
            return "content", "image_or_form_invocation", 0
        if operator in {b"S", b"s", b"B", b"B*", b"b", b"b*", b"sh"}:
            return "content", "stroke_or_shading_present", 0
        if operator == b"gs":
            return "review_required", "external_graphics_state_or_opacity", 0
        if operator == b"q":
            saved.append((fill_white, matrix))
        elif operator == b"Q":
            if not saved:
                return "review_required", "unbalanced_graphics_state", 0
            fill_white, matrix = saved.pop()
        elif operator == b"BT":
            if in_text:
                return "review_required", "unbalanced_text_state", 0
            in_text = True
        elif operator == b"ET":
            if not in_text:
                return "review_required", "unbalanced_text_state", 0
            in_text = False
        elif operator in {b"g", b"rg", b"k", b"cm", b"re"}:
            values = [float(value) for value in operands]
            if not all(math.isfinite(value) for value in values):
                return "review_required", "nonfinite_graphics_operand", 0
            if operator in {b"g", b"rg", b"k"}:
                white = {b"g": [1.], b"rg": [1., 1., 1.], b"k": [0., 0., 0., 0.]}
                if len(values) != len(white[operator]) or any(value < 0 or value > 1 for value in values):
                    return "review_required", "unsupported_fill_color", 0
                fill_white = values == white[operator]
            elif operator == b"cm":
                if len(values) != 6:
                    return "review_required", "invalid_graphics_matrix", 0
                a, b, c, d, e, f = values
                A, B, C, D, E, F = matrix
                matrix = (a*A+b*C, a*B+b*D, c*A+d*C, c*B+d*D, e*A+f*C+E, e*B+f*D+F)
                if not all(math.isfinite(value) for value in matrix) or matrix[0]*matrix[3] == matrix[1]*matrix[2]:
                    return "review_required", "invalid_graphics_matrix", 0
            else:
                if len(values) != 4 or values[2] <= 0 or values[3] <= 0:
                    return "review_required", "unsupported_rectangle_geometry", 0
                x, y, width, height = values
                a, b, c, d, e, f = matrix
                corners = [(a*X+c*Y+e, b*X+d*Y+f) for X, Y in ((x,y),(x+width,y),(x,y+height),(x+width,y+height))]
                if any(not (box[0] <= X <= box[2] and box[1] <= Y <= box[3]) for X,Y in corners):
                    return "review_required", "rectangle_outside_media_box", 0
                path.append(values)
        elif operator in {b"f", b"f*", b"F"}:
            if not path:
                return "review_required", "unsupported_fill_geometry", 0
            if not fill_white:
                return "content", "nonwhite_vector_fill", 0
            rectangles += len(path)
            path = []
        elif operator == b"n":
            path = []
        elif operator not in NON_PAINT_OPERATORS:
            return "review_required", "unsupported_content_operator", 0
    if saved or in_text or path:
        return "review_required", "incomplete_graphics_or_text_state", 0
    return "blank_candidate", "opaque_white_rectangles_only" if rectangles else "nonpainting_content_only", rectangles


def _diagnostic(source_hash: str, page: int, classification: str, reason: str, proof: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"schema_version": DIAGNOSTIC_SCHEMA, "source_sha256": source_hash,
            "physical_page": page, "classification": classification, "reason_codes": [reason], "proof": proof,
            "page_role": "knowledge-candidate" if classification=="native" else "blank" if classification=="confirmed_blank" else "unresolved",
            "source_type": "native-text" if classification=="native" else "nontext" if classification=="confirmed_blank" else "unknown"}


def native_text_diagnostic(source_hash: str, page: int) -> dict[str, Any]:
    return _diagnostic(source_hash, page, "native", "native_text_usable")


def has_image_invocation(page: Any) -> bool:
    """Detect invoked raster content, including images inside invoked Forms."""
    from pypdf.generic import ContentStream
    seen: set[int] = set()

    def visit(content: Any, resources: Any, depth: int) -> bool:
        if content is None or depth > 8:
            return False
        resources = resources.get_object() if hasattr(resources, "get_object") else resources
        objects = resources.get("/XObject", {}) if resources else {}
        objects = objects.get_object() if hasattr(objects, "get_object") else objects
        for operands, operator in ContentStream(content, getattr(page, "pdf", None)).operations:
            if operator == b"INLINE IMAGE":
                return True
            if operator != b"Do" or len(operands) != 1 or operands[0] not in objects:
                continue
            obj = objects[operands[0]].get_object()
            if obj.get("/Subtype") == "/Image":
                return True
            if obj.get("/Subtype") == "/Form" and id(obj) not in seen:
                seen.add(id(obj))
                if visit(obj, obj.get("/Resources", resources), depth + 1):
                    return True
        return False

    try:
        return visit(page.get_contents(), page.get("/Resources", {}), 0)
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def summarize_page_diagnostics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    categories = ("native", "confirmed_blank", "needs_ocr", "review_required")
    return {"page_count": len(rows), "counts": {kind: sum(row["classification"] == kind for row in rows) for kind in categories},
            "pages": {kind: [row["physical_page"] for row in rows if row["classification"] == kind] for kind in categories}}


def native_ranges(rows: list[dict[str, Any]]) -> list[list[int]]:
    """Continuous safe scopes; a blank-only island cannot create knowledge."""
    ranges: list[list[int]] = []
    current: list[dict[str, Any]] = []
    def finish() -> None:
        if current and any(row["classification"] == "native" and not row.get("disposition_proof") for row in current):
            ranges.append([current[0]["physical_page"], current[-1]["physical_page"]])
        current.clear()
    for row in rows:
        if row["classification"] in {"native", "confirmed_blank"}:
            current.append(row)
        else:
            finish()
    finish()
    return ranges


def diagnose_blank_page(
    page: Any,
    raw_text: str,
    source: Path,
    source_sha256: str,
    physical_page: int,
    renderer: Path | str | None = None,
) -> dict[str, Any]:
    """Return explicit content, confirmed_blank or review_required diagnostics.

    No threshold or fuzzy white-pixel rule is used. Raster scans of blank paper,
    white images, invisible marks, short text and annotations remain unconfirmed.
    A missing/broken local renderer also never grants a blank-page exemption.
    """
    def reject(reason: str) -> dict[str, Any]:
        return _diagnostic(source_sha256, physical_page, "review_required", reason)
    try:
        from pypdf import __version__ as pypdf_version
        from pypdf.generic import ContentStream

        classification, reason, white_rectangles = _content_diagnostic(page, raw_text)
        from native_print_marks import outside_print_marks, PROTOCOL as PRINT_MARK_PROTOCOL
        content = page.get_contents()
        operations = ContentStream(content, getattr(page, "pdf", None)).operations if content is not None else []
        outside_marks = False
        if classification != "blank_candidate" and not raw_text.strip() and not any(page.get(k) for k in ("/Annots", "/AA", "/Group", "/PresSteps")):
            outside_marks = outside_print_marks(page, operations)
            if outside_marks:
                classification, reason = "blank_candidate", "outside_media_print_marks_only"
        if classification != "blank_candidate":
            return _diagnostic(source_sha256, physical_page, classification, reason)
        content = page.get_contents()
        operations = ContentStream(content, getattr(page, "pdf", None)).operations if content is not None else []
        # Refuse malformed/unbalanced streams even when their operators do not
        # paint. Parsing/rendering disagreement must not qualify an exemption.
        stack, in_text = 0, False
        for _operands, operator in operations:
            if operator == b"q":
                stack += 1
            elif operator == b"Q":
                stack -= 1
                if stack < 0:
                    return reject("unbalanced_graphics_state")
            elif operator == b"BT":
                if in_text:
                    return reject("unbalanced_text_state")
                in_text = True
            elif operator == b"ET":
                if not in_text:
                    return reject("unbalanced_text_state")
                in_text = False
        if stack or in_text:
            return reject("unbalanced_graphics_state")
        media_box = [float(value) for value in page.mediabox]
        if not all(math.isfinite(value) for value in media_box):
            return reject("invalid_page_geometry")
        rotation = int(page.get("/Rotate", 0))
        unit = float(page.get("/UserUnit", 1))
        if rotation not in {0, 90, 180, 270} or unit != 1:
            return reject("unsupported_page_geometry")
        width, height = media_box[2] - media_box[0], media_box[3] - media_box[1]
        if rotation in {90, 270}:
            width, height = height, width
        if width <= 0 or height <= 0 or math.ceil(width * DPI / 72) * math.ceil(height * DPI / 72) > 25_000_000:
            return reject("unsupported_page_geometry")
        selected = renderer if renderer is not None else shutil.which("pdftoppm")
        if selected is None:
            return reject("renderer_missing")
        executable = Path(selected).expanduser()
        if not executable.is_file():
            found = shutil.which(str(selected))
            if not found:
                return reject("renderer_missing")
            executable = Path(found)
        executable = executable.resolve()
        renderer_hash = _digest(executable)
        version = subprocess.run([str(executable), "-v"], capture_output=True, text=True, timeout=10)
        version_lines = (version.stderr or version.stdout).strip().splitlines()
        if version.returncode or not version_lines:
            return reject("renderer_version_unavailable")
        with tempfile.TemporaryDirectory(prefix="tkc-native-blank-") as temporary:
            prefix = Path(temporary) / "full-page"
            rendered = subprocess.run(
                [str(executable), "-f", str(physical_page), "-l", str(physical_page),
                 "-singlefile", "-r", str(DPI), str(source), str(prefix)],
                capture_output=True, timeout=30,
            )
            target = prefix.with_suffix(".ppm")
            if rendered.returncode or rendered.stderr.strip() or not target.is_file():
                return reject("renderer_failed" if rendered.returncode or not target.is_file() else "renderer_warning")
            data = target.read_bytes()
        dimensions = _white_ppm(data)
        if dimensions != (math.ceil(width * DPI / 72), math.ceil(height * DPI / 72)):
            return reject("render_not_strict_white_or_geometry_mismatch")
        if _digest(source) != source_sha256 or _digest(executable) != renderer_hash:
            return reject("source_or_renderer_drift")
        decoded_content = content.get_data() if content is not None else b""
        proof = {
            "protocol": PRINT_MARK_PROTOCOL if outside_marks else WHITE_VECTOR_PROTOCOL if white_rectangles else PROTOCOL,
            "source_sha256": source_sha256,
            "physical_page": physical_page,
            "pypdf_version": pypdf_version,
            "content_sha256": hashlib.sha256(decoded_content).hexdigest(),
            "content_operator_count": len(operations),
            "proof": "outside-media-print-marks-and-exact-white-full-render" if outside_marks else "opaque-white-rectangles-and-exact-white-full-render" if white_rectangles else "non-painting-content-and-exact-white-full-render",
            "render": {
                "renderer": "pdftoppm", "version": version_lines[0],
                "renderer_sha256": renderer_hash, "dpi": DPI, "format": "ppm-rgb8",
                "box": "MediaBox", "media_box_points": media_box,
                "rotation_clockwise": rotation, "pixel_bounds": [0, 0, *dimensions],
                "sha256": hashlib.sha256(data).hexdigest(),
            },
        }
        if white_rectangles:
            proof["white_rectangle_count"] = white_rectangles
        return _diagnostic(source_sha256, physical_page, "confirmed_blank", reason, proof)
    except subprocess.TimeoutExpired:
        return reject("renderer_timeout")
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError):
        return reject("blank_inspection_failed")


def confirm_blank_page(page: Any, raw_text: str, source: Path, source_sha256: str,
                       physical_page: int, renderer: Path | str | None = None) -> dict[str, Any] | None:
    """Compatibility API; new agent-facing callers use explicit diagnostics."""
    return diagnose_blank_page(page, raw_text, source, source_sha256, physical_page, renderer)["proof"]
