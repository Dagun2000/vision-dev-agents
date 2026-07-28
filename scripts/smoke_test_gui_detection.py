"""Manual smoke test for GUI Tester step 4-1: real screenshot capture +
OpenCV clickable-element detection + Set-of-marks label overlay.

This is NOT headless -- it serves target-app/ on localhost, opens it in
your actual default browser (maximized, then F11 fullscreen), and takes a
real screenshot of your screen with PyAutoGUI. Don't use the mouse/keyboard
while it runs.

Usage:
    uv run python scripts/smoke_test_gui_detection.py
"""

from __future__ import annotations

from agents.gui.execution import enable_windows_dpi_awareness
from agents.gui_tester import OpenAIGUITesterAgent
from orchestrator.config import PipelineConfig
from orchestrator.encoding import ensure_utf8_stdio


def main() -> None:
    ensure_utf8_stdio()
    enable_windows_dpi_awareness()

    config = PipelineConfig()
    gui_tester = OpenAIGUITesterAgent(config=config)

    raw_path, labeled_path, boxes, _labeled_image, _window = gui_tester.capture_labeled_screenshot()

    print(f"raw screenshot: {raw_path}")
    print(f"labeled screenshot: {labeled_path}")
    print(f"감지된 후보 요소 {len(boxes)}개:")
    for box in boxes:
        print(f"  #{box.index}: x={box.x} y={box.y} w={box.width} h={box.height}")


if __name__ == "__main__":
    main()
