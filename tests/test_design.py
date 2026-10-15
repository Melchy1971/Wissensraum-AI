from pathlib import Path
import tempfile
import unittest

from PIL import Image

import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from design import DesignSettings


class DesignSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.base=Path(self.temp.name)
        self.design=DesignSettings(self.base)

    def tearDown(self):
        self.temp.cleanup()

    def test_theme_is_validated_and_persisted(self):
        self.assertEqual(self.design.theme(),'light')
        self.design.save_theme('dark')
        self.assertEqual(DesignSettings(self.base).theme(),'dark')
        with self.assertRaisesRegex(ValueError,'hell oder dunkel'):
            self.design.save_theme('automatic')

    def test_logo_is_normalized_and_removable(self):
        source=self.base/'large-logo.jpg'
        Image.new('RGB',(1200,300),'#e20074').save(source)
        saved=self.design.save_logo(source)
        self.assertEqual(saved,self.design.logo_path)
        with Image.open(saved) as logo:
            self.assertEqual(logo.format,'PNG')
            self.assertLessEqual(logo.width,320)
            self.assertLessEqual(logo.height,96)
        invalid=self.base/'invalid.png'
        invalid.write_text('not an image',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'kein lesbares'):
            self.design.save_logo(invalid)
        self.design.remove_logo()
        self.assertFalse(saved.exists())


if __name__=='__main__': unittest.main()
