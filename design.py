"""Local appearance preferences and normalized application logo storage."""
from __future__ import annotations

import json
from pathlib import Path

THEMES = {
    'light': {
        'background':'#f5f5f5', 'surface':'#ffffff', 'input':'#ffffff',
        'text':'#1b1b1b', 'muted':'#5f5f5f', 'border':'#a4a4a4',
        'accent':'#e20074', 'accent_active':'#bd0061', 'disabled':'#8f6f80',
        'danger':'#b3262e', 'danger_active':'#8e1e25', 'success':'#168044',
    },
    'dark': {
        'background':'#121212', 'surface':'#202020', 'input':'#2a2a2a',
        'text':'#f5f5f5', 'muted':'#b8b8b8', 'border':'#626262',
        'accent':'#e20074', 'accent_active':'#ff2494', 'disabled':'#75596a',
        'danger':'#c53b43', 'danger_active':'#e2555d', 'success':'#57b881',
    },
}


class DesignSettings:
    MAX_LOGO_BYTES = 5*1024*1024
    MAX_SOURCE_PIXELS = 20_000_000
    LOGO_SIZE = (320,96)

    def __init__(self,base):
        self.base=Path(base)
        self.directory=self.base/'data'/'branding'
        self.path=self.base/'data'/'design.json'
        self.logo_path=self.directory/'logo.png'

    def values(self):
        try:
            values=json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError,ValueError,TypeError):
            values={}
        theme=values.get('theme','light')
        return {'theme':theme if theme in THEMES else 'light'}

    def theme(self):
        return self.values()['theme']

    def save_theme(self,theme):
        if theme not in THEMES:
            raise ValueError('Designmodus muss hell oder dunkel sein.')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'theme':theme},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        temporary.replace(self.path)

    def save_logo(self,source):
        source=Path(source)
        try:
            from PIL import Image, UnidentifiedImageError
        except Exception:
            raise ValueError('Die Bildbibliothek ist beschädigt. Anwendung über start_windows.bat neu starten, damit die Umgebung repariert wird.') from None
        try:
            if not source.is_file():
                raise ValueError('Die ausgewählte Logodatei ist nicht verfügbar.')
            if source.stat().st_size > self.MAX_LOGO_BYTES:
                raise ValueError('Das Logo darf maximal 5 MB groß sein.')
            with Image.open(source) as image:
                width,height=image.size
                if not width or not height or width*height > self.MAX_SOURCE_PIXELS:
                    raise ValueError('Das Logo darf maximal 20 Millionen Bildpunkte enthalten.')
                image.load()
                normalized=image.convert('RGBA')
                normalized.thumbnail(self.LOGO_SIZE,Image.Resampling.LANCZOS)
                self.directory.mkdir(parents=True,exist_ok=True)
                temporary=self.directory/'logo.tmp.png'
                normalized.save(temporary,format='PNG',optimize=True)
                temporary.replace(self.logo_path)
        except ValueError:
            raise
        except (OSError,UnidentifiedImageError):
            raise ValueError('Die Datei ist kein lesbares PNG-, JPG- oder WebP-Bild.') from None
        return self.logo_path

    def remove_logo(self):
        try:
            self.logo_path.unlink()
        except FileNotFoundError:
            pass

