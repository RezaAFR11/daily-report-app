import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageOps
from monthly_report.renderer import _photo_grid_table, _styles


class BalancedPhotoLayoutTests(unittest.TestCase):
    def test_cards_align_with_long_short_and_empty_captions(self):
        with tempfile.TemporaryDirectory() as temp:
            rows = []
            for index, size in enumerate([(320, 240), (100, 250), (120, 80)]):
                asset = str(index + 1) * 64
                Image.new('RGB', size, 'blue').save(Path(temp, asset + '.jpg'))
                rows.append({'asset_id': asset, 'source_area': 'MA-77' if index else 'Long area title ' * 5,
                             'caption': ['Testing', 'Detailed verified caption ' * 15, ''][index]})
            for compact in [True, False]:
                grid = _photo_grid_table(rows, _styles(), root=temp, compact=compact,
                                         image_module=Image, image_ops=ImageOps)
                cards = grid._cellvalues[0]
                sizes = [card.wrap(200, 10000) for card in cards]
                self.assertEqual(len(set(height for _, height in sizes)), 1)
                self.assertEqual(cards[0]._rowHeights, cards[1]._rowHeights)
                self.assertEqual(cards[1]._rowHeights, cards[2]._rowHeights)
                for card in cards:
                    rendered = card._cellvalues[-1][0]
                    if isinstance(rendered, (tuple, list)):
                        rendered = rendered[0]
                    pixels = Image.frombytes('RGB', rendered._img.getSize(), rendered._img.getRGBData())
                    # Even small/portrait images must reach every frame edge,
                    # without white margins introduced by the renderer.
                    for point in [(0, 0), (pixels.width - 1, 0), (0, pixels.height - 1),
                                  (pixels.width - 1, pixels.height - 1)]:
                        red, green, blue = pixels.getpixel(point)
                        self.assertLess(red, 10)
                        self.assertLess(green, 10)
                        self.assertGreater(blue, 240)
                    for index in [0, 1]:
                        paragraph = card._cellvalues[index][0]
                        if isinstance(paragraph, (tuple, list)):
                            paragraph = paragraph[0]
                        available = card._colWidths[0] - 2 * (2 if compact else 3)
                        self.assertLessEqual(paragraph.wrap(available, 10000)[1], card._rowHeights[index])


if __name__ == '__main__':
    unittest.main()
