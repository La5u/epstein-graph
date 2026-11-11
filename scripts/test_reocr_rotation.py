"""Fast, corpus-free checks for the rotation pilot's image gate and word scoring."""
import unittest

from PIL import Image, ImageDraw, ImageFont

from reocr_rotation import clearly_better, orientation, recovered_entities, sideways_label, word_score


class RotationTest(unittest.TestCase):
    def text_page(self):
        image = Image.new('L', (640, 820), 255)
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default(size=16)
        for row in range(16):
            draw.text((30 + row % 5 * 7, 35 + row * 35),
                      'The quick brown fox jumps over the lazy dog.'[:35 + row % 9], font=font, fill=0)
        return image

    def test_projection_turns(self):
        image = self.text_page()
        self.assertEqual(orientation(image)['class'], 'upright')
        for angle in (90, 270):
            self.assertEqual(orientation(image.rotate(angle, expand=True))['class'], 'sideways')
        self.assertEqual(orientation(image.rotate(180))['class'], 'upright')

    def test_blank_and_solid_rule(self):
        image = Image.new('L', (600, 800), 255)
        self.assertEqual(orientation(image)['class'], 'blank')
        ImageDraw.Draw(image).rectangle((10, 200, 590, 205), fill=0)
        self.assertEqual(orientation(image)['class'], 'blank')

    def test_scoring_and_gain(self):
        words = {'the', 'quick', 'brown', 'fox'}
        score = word_score('The quick brown fox zzqq 12 an', words)
        self.assertEqual(score['words'], 5)
        self.assertEqual(score['dictionary_words'], 4)
        self.assertEqual(score['ratio'], .8)
        self.assertEqual(score['score'], 3.2)
        self.assertEqual(word_score('', words)['score'], 0)
        good = word_score('The quick brown fox ' * 6, words)
        self.assertTrue(clearly_better(good, score))
        self.assertFalse(clearly_better(good, good))

    def test_entity_boundaries_and_private_emails(self):
        index = {'jane': [('jane doe', 'persons', '123')], 'example': [('example inc', 'companies', 'company_abc')]}
        r = recovered_entities('Jane Doe, Example, Inc. new@example.com OLD@example.com',
                               'Example Inc old@example.com', index, b'test')
        self.assertEqual(r['persons'], ['123'])
        self.assertEqual(r['companies'], [])
        self.assertEqual(len(r['emails']), 1)
        self.assertTrue(r['emails'][0].startswith('email_'))
        self.assertNotIn('@', r['emails'][0])
        self.assertEqual(recovered_entities('Jane Does', '', index, b'test')['persons'], [])

    def test_axis_label_does_not_require_90_270_separation(self):
        words = {'the', 'quick', 'brown', 'fox'}
        good = word_score('The quick brown fox ' * 10, words)
        bad = word_score('zzqq', words)
        self.assertTrue(sideways_label({0: bad, 180: bad, 90: good, 270: good}))
        self.assertFalse(sideways_label({0: good, 180: good, 90: bad, 270: bad}))
        self.assertIsNone(sideways_label({a: good for a in (0, 90, 180, 270)}))


if __name__ == '__main__':
    unittest.main()
