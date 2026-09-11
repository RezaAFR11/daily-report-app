import io
import tempfile
import unittest
from pathlib import Path

import fitz
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate

from monthly_report.renderer import _photo_grid_flowables, _styles, BODY_LEFT, BODY_RIGHT, BODY_TOP_MARGIN, BODY_BOTTOM


class PhotoDatePaginationTests(unittest.TestCase):
    def render(self, groups, caption='Checked equipment'):
        with tempfile.TemporaryDirectory() as temp:
            photos = []
            for date, count in groups:
                for index in range(count):
                    asset = f'{len(photos) + 1:064x}'
                    Image.new('RGB', (90, 70), (index * 20, 100, 160)).save(Path(temp, asset + '.jpg'))
                    photos.append({'asset_id': asset, 'source_date': date, 'order': index,
                                   'source_area': 'MA-77', 'caption': caption})
            buffer = io.BytesIO()
            SimpleDocTemplate(buffer, pagesize=A4, leftMargin=BODY_LEFT, rightMargin=BODY_RIGHT,
                              topMargin=BODY_TOP_MARGIN, bottomMargin=BODY_BOTTOM).build(
                _photo_grid_flowables(photos, _styles(), photo_base_dir=temp))
            return fitz.open(stream=buffer.getvalue(), filetype='pdf')

    def test_dates_share_page_but_partial_rows_stay_separate(self):
        doc = self.render([('2026-08-10', 2), ('2026-08-11', 1), ('2026-08-12', 3)])
        self.assertEqual(len(doc), 1)
        page = doc[0]
        photos = page.get_image_info()
        counts = {}
        for image in photos:
            y = round(image['bbox'][1], 1)
            counts[y] = counts.get(y, 0) + 1
        self.assertEqual(list(counts.values()), [2, 1, 3])
        for date, y in zip(['2026-08-10', '2026-08-11', '2026-08-12'], counts):
            self.assertLess(page.search_for('Photo Documentation: ' + date)[0].y1, y)

    def test_four_rows_max_and_continuation_heading(self):
        doc = self.render([('2026-08-10', 2), ('2026-08-11', 12)])
        self.assertEqual(len(doc), 2)
        self.assertEqual([len(p.get_image_info()) for p in doc], [11, 3])
        self.assertIn('Photo Documentation: 2026-08-11 (continued)', doc[1].get_text())

    def test_long_captions_break_before_whole_row_with_its_heading(self):
        doc = self.render([('2026-08-10', 3), ('2026-08-11', 3), ('2026-08-12', 3)],
                          caption='Verified pressure switch calibration and equipment function. ' * 7)
        self.assertGreater(len(doc), 1)
        self.assertEqual(sum(len(p.get_image_info()) for p in doc), 9)
        for page in doc:
            self.assertTrue(page.get_image_info())
            self.assertIn('Photo Documentation:', page.get_text())
            for image in page.get_image_info():
                self.assertLessEqual(image['bbox'][3], A4[1] - BODY_BOTTOM + 1)


if __name__ == '__main__':
    unittest.main()
