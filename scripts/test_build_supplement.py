import unittest

from build_supplement import image_text


class ImageTextTests(unittest.TestCase):
    def test_keeps_text_drops_answers_about_the_image(self):
        self.assertEqual(image_text('The text in the image reads: CSAM NOT SCANNED 18 Thumbnails'), 'CSAM NOT SCANNED 18 Thumbnails')
        self.assertEqual(image_text('```\nDATE 7 / 6 / 19\nLOCATION 9 East 71st St\n```'), 'DATE 7 / 6 / 19\nLOCATION 9 East 71st St')
        for answer in ['There is no visible text in the image.', 'The text in the image is not legible due to the resolution.',
                       'There is a black square on the body of one of the individuals, obscuring any text that might be present.',
                       '[any visible text]', None]:
            self.assertIsNone(image_text(answer))


if __name__ == '__main__':
    unittest.main()
