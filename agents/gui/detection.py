"""Visual-container detection and Set-of-marks label overlay.

Pure image processing: takes a screenshot (PIL Image) and returns
candidate bounding boxes plus a labeled copy of the image. No OS,
browser, or DOM APIs here -- this is judgment-side preprocessing, fed to
the Vision model together with the Phase's success_criteria in a later
step so it can answer in terms of "element N" instead of guessing pixel
coordinates.

Detection targets *visual containers* only -- rectangular regions with a
stroke (border) or a fill (uniform background distinct from what's behind
it), such as buttons, inputs, and cards. Plain text (headings, captions,
empty-state messages) has neither and is deliberately excluded here; a
separate OCR pass (agents/gui/ocr.py) reads text *inside* the boxes kept
by this module to describe them, but never creates its own candidate boxes.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image

MIN_BOX_WIDTH = 24
MIN_BOX_HEIGHT = 16
# A candidate must enclose at least this fraction of its own bounding-box
# area to count as a "clean rectangle" (stroke/fill), which is what
# separates real boxes (buttons/inputs measured ~0.94-0.99 in testing)
# from sparse, irregular text glyph clusters (measured ~0.6-0.75).
MIN_EXTENT = 0.8
# A rectangle (allowing for rounded corners) simplifies to few vertices;
# text blobs generally don't.
MAX_APPROX_VERTICES = 10
# Ignore boxes covering more than this fraction of the screenshot -- those
# are page/card frames, not individually clickable controls.
MAX_BOX_AREA_RATIO = 0.7
MAX_ELEMENTS = 30
CONTAINMENT_TOLERANCE_PX = 2

# A pixel counts as "changed" if any RGB channel moves by more than this.
PIXEL_CHANGE_THRESHOLD = 20
# Screens count as "changed" once at least this fraction of pixels differ --
# filters out things like a blinking text-input caret.
SCREEN_CHANGE_RATIO = 0.01


@dataclass
class BoundingBox:
    index: int
    x: int
    y: int
    width: int
    height: int

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + self.width // 2, self.y + self.height // 2)

    @property
    def rect(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.width, self.height)


def _is_contained(outer: tuple[int, int, int, int], inner: tuple[int, int, int, int]) -> bool:
    """True if `inner` sits fully inside `outer` and is strictly smaller."""
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    t = CONTAINMENT_TOLERANCE_PX
    fits = (
        ix >= ox - t
        and iy >= oy - t
        and ix + iw <= ox + ow + t
        and iy + ih <= oy + oh + t
    )
    return fits and (iw * ih) < (ow * oh)


def _suppress_containers(boxes: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    """Drop any box that fully contains another (smaller) candidate box,
    keeping only the innermost ones -- e.g. an input's own border vs. a
    stray duplicate contour just outside it, or (before OCR-only text) an
    input container vs. its placeholder text box."""
    return [
        box
        for i, box in enumerate(boxes)
        if not any(j != i and _is_contained(box, other) for j, other in enumerate(boxes))
    ]


def detect_clickable_elements(image: Image.Image) -> list[BoundingBox]:
    img_rgb = np.array(image.convert("RGB"))
    img_bgr = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)

    # Low thresholds + a real morphological close (not just dilate) so
    # low-contrast borders (e.g. a light-grey 1px input outline) still
    # trace as one continuous closed loop instead of a broken contour.
    #
    # Kernel size/iterations matter a lot here: a (5,5) kernel x2 iterations
    # (confirmed by direct testing) bridges gaps up to ~10-13px, which is
    # enough to merge two visually-separate adjacent buttons into one
    # contour whenever their gap is smaller than that -- confirmed on a
    # real case (two 7px-apart card-action buttons, each with its own
    # border, detected as a single box). (3,3) x1 iteration still closes a
    # single low-contrast border into one loop and still correctly detects
    # a solid-fill button, but no longer bridges a 7px inter-element gap.
    edges = cv2.Canny(gray, 20, 60)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=1)

    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    height, width = gray.shape[:2]
    image_area = width * height

    candidates: list[tuple[int, int, int, int]] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w < MIN_BOX_WIDTH or h < MIN_BOX_HEIGHT:
            continue
        if (w * h) > image_area * MAX_BOX_AREA_RATIO:
            continue

        rect_area = w * h
        contour_area = cv2.contourArea(contour)
        extent = contour_area / rect_area if rect_area > 0 else 0.0
        if extent < MIN_EXTENT:
            continue  # sparse/irregular shape -- text, not a stroke/fill box

        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
        if len(approx) > MAX_APPROX_VERTICES:
            continue  # not rectangle-ish even allowing for rounded corners

        candidates.append((x, y, w, h))

    # Larger first, so containment suppression below has stable "outer
    # candidate found" behavior regardless of contour discovery order.
    candidates.sort(key=lambda box: box[2] * box[3], reverse=True)
    kept = _suppress_containers(candidates)
    kept = kept[:MAX_ELEMENTS]

    # Reading order (top-to-bottom, then left-to-right) for stable,
    # human-legible numbering. Rows are bucketed loosely so elements that
    # are roughly on the same line don't get shuffled by a few pixels.
    kept.sort(key=lambda box: (round(box[1] / 20), box[0]))

    return [
        BoundingBox(index=i + 1, x=x, y=y, width=w, height=h) for i, (x, y, w, h) in enumerate(kept)
    ]


def overlay_labels(image: Image.Image, boxes: list[BoundingBox]) -> Image.Image:
    img_rgb = np.array(image.convert("RGB"))
    img_bgr = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)

    box_color = (0, 200, 0)  # BGR
    badge_color = (0, 0, 220)
    text_color = (255, 255, 255)
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.55
    thickness = 2

    for box in boxes:
        cv2.rectangle(
            img_bgr, (box.x, box.y), (box.x + box.width, box.y + box.height), box_color, 2
        )

        label = str(box.index)
        (text_w, text_h), baseline = cv2.getTextSize(label, font, font_scale, thickness)
        badge_x = box.x
        badge_y = max(box.y - text_h - baseline - 4, 0)

        cv2.rectangle(
            img_bgr,
            (badge_x, badge_y),
            (badge_x + text_w + 8, badge_y + text_h + baseline + 6),
            badge_color,
            cv2.FILLED,
        )
        cv2.putText(
            img_bgr,
            label,
            (badge_x + 4, badge_y + text_h + 2),
            font,
            font_scale,
            text_color,
            thickness,
            cv2.LINE_AA,
        )

    return Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))


def padded_region(
    box: BoundingBox, padding: int, image_size: tuple[int, int]
) -> tuple[int, int, int, int]:
    """box's rect, expanded by `padding` px and clamped to image_size -- an
    (x, y, w, h) region suitable for Image.crop / screenshots_differ."""
    width, height = image_size
    x1 = max(box.x - padding, 0)
    y1 = max(box.y - padding, 0)
    x2 = min(box.x + box.width + padding, width)
    y2 = min(box.y + box.height + padding, height)
    return (x1, y1, x2 - x1, y2 - y1)


def screenshots_differ(
    before: Image.Image, after: Image.Image, region: tuple[int, int, int, int] | None = None
) -> bool:
    """Cheap pixel-diff check: did the screen (or just `region`, if given)
    actually change?

    Used after executing an action -- if nothing changed, the action likely
    had no effect (wrong coordinates, element wasn't actually interactive,
    etc). A small, localized change (e.g. text appearing in one input box)
    can be far under the threshold for a full 1920x1080 screenshot, so
    callers that know the action only affects one element (e.g. "type")
    should pass that element's region instead of comparing the whole screen.
    """
    if region is not None:
        x, y, w, h = region
        before = before.crop((x, y, x + w, y + h))
        after = after.crop((x, y, x + w, y + h))

    if before.size != after.size:
        return True

    before_arr = np.array(before.convert("RGB"), dtype=np.int16)
    after_arr = np.array(after.convert("RGB"), dtype=np.int16)
    changed_pixels = np.any(np.abs(before_arr - after_arr) > PIXEL_CHANGE_THRESHOLD, axis=-1)
    return bool(changed_pixels.mean() > SCREEN_CHANGE_RATIO)
