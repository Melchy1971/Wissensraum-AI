from pathlib import Path
import builtins
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.venv' / 'Lib' / 'site-packages'))
sys.path.insert(0, str(ROOT))
from PIL import Image

from configuration_authority import configuration_operation_lock
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

    def test_theme_write_fails_closed_while_configuration_operation_is_owned(self):
        lock=configuration_operation_lock(self.base)
        lock.acquire()
        try:
            with self.assertRaises(BlockingIOError):
                DesignSettings(self.base).save_theme('dark')
            self.assertFalse(self.design.path.exists())
        finally:
            lock.release()
        self.design.save_theme('dark')
        self.assertEqual(self.design.theme(),'dark')

    def test_theme_write_uses_unique_temporary_and_syncs_before_replace(self):
        collision=self.design.path.with_suffix('.tmp')
        collision.parent.mkdir(parents=True,exist_ok=True)
        collision.write_bytes(b'owned-by-another-process')
        operations=[]
        sources=[]
        real_replace=os.replace
        def replace(source,target):
            sources.append(Path(source))
            operations.append('replace')
            return real_replace(source,target)
        with patch('design.os.fsync',side_effect=lambda _fd:operations.append('file')), \
             patch('design.os.replace',side_effect=replace), \
             patch('design._sync_directory',side_effect=lambda _path:operations.append('directory')):
            self.design.save_theme('dark')
        self.assertEqual(operations[-3:],['file','replace','directory'])
        self.assertEqual(len(sources),1)
        self.assertNotEqual(sources[0],collision)
        self.assertEqual(collision.read_bytes(),b'owned-by-another-process')
        self.assertEqual(json.loads(self.design.path.read_text(encoding='utf-8')),
                         {'theme':'dark'})

    def test_theme_write_cleans_unique_temporary_after_baseexception(self):
        fixed_uuid=type('FixedUuid',(),{'hex':'1'*32})()
        temporary=self.design.path.with_name(self.design.path.name+'.tmp-'+'1'*32)
        self.design.path.parent.mkdir(parents=True,exist_ok=True)
        self.design.path.write_bytes(b'previous')
        interrupted=KeyboardInterrupt('interrupted')
        with patch('design.uuid.uuid4',return_value=fixed_uuid), \
             patch('design.os.replace',side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.design.save_theme('dark')
        self.assertIs(caught.exception,interrupted)
        self.assertFalse(temporary.exists())
        self.assertEqual(self.design.path.read_bytes(),b'previous')

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
        original_import=builtins.__import__
        def broken_pillow(name,*args,**kwargs):
            if name=='PIL':
                raise SystemError('broken native module')
            return original_import(name,*args,**kwargs)
        with patch('builtins.__import__',side_effect=broken_pillow),self.assertRaisesRegex(ValueError,'Bildbibliothek ist beschädigt'):
            self.design.save_logo(source)
        self.design.remove_logo()
        self.assertFalse(saved.exists())


if __name__=='__main__': unittest.main()
