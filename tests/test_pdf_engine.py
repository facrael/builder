import sys
import unittest
from pathlib import Path
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from pdf_engine import (
    normalize_code,
    detect_dsp_marker,
    apply_diagonal_watermark,
)


class TestPDFEngine(unittest.TestCase):
    def test_normalize_code(self):
        self.assertEqual(normalize_code("КР-12"), "КР12")
        self.assertEqual(normalize_code("KP-12"), "КР12")  # Latin K and P -> Cyrillic К and Р
        self.assertEqual(normalize_code("ЦКДИ.2981.КЖ"), "ЦКДИ2981КЖ")
        self.assertEqual(normalize_code("АР-3"), "АР3")
        self.assertEqual(normalize_code("AP3"), "АР3")

    def test_detect_dsp_marker_in_filename(self):
        self.assertTrue(detect_dsp_marker("", "Э1075_ДСП_ЦТП_КМ10.pdf"))
        self.assertTrue(detect_dsp_marker("", "Э756дспИ2.pdf"))
        self.assertTrue(detect_dsp_marker("", "проект_для_служебного_пользования.pdf"))
        self.assertFalse(detect_dsp_marker("", "чертеж_КЖ_открытый.pdf"))
        self.assertFalse(detect_dsp_marker("", "kns.pdf"))

    def test_apply_diagonal_watermark(self):
        img = Image.new("RGB", (800, 600), (255, 255, 255))
        watermarked = apply_diagonal_watermark(img, "Иванов И.И. · 10.11.2026 · ЦТП КЖ Изм.1")
        self.assertEqual(watermarked.size, (800, 600))
        extrema = watermarked.getextrema()
        self.assertTrue(any(low < 255 for low, high in extrema))


if __name__ == "__main__":
    unittest.main()
