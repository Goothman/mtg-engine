import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")
from mtg_engine.vision.recognizer import detect_cards  # noqa: E402


def test_detects_two_cards_on_table():
    img = np.full((700, 900, 3), (40, 80, 120), np.uint8)
    for x, y in ((300, 150), (600, 250)):
        img[y : y + 352, x : x + 252] = (12, 12, 12)
        img[y + 40 : y + 200, x + 20 : x + 232] = (200, 120, 60)
    found = detect_cards(img)
    assert len(found) == 2
    assert all(crop.shape[:2] == (352, 252) for crop, _ in found)
    assert sorted(b[0] for _, b in found) == [299, 599]
