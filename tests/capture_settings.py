"""CI capture contains only synthetic credentials and no personal documents."""
from pathlib import Path
import sys
import os
import tempfile
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import app
from settings import Settings
from test_settings import FakeVault
from PIL import ImageGrab

with tempfile.TemporaryDirectory() as directory:
    base=Path(directory)
    (base/'.env.local').write_text('OPENAI_API_KEY=synthetic-existing-1234567890\nANTHROPIC_API_KEY=synthetic-claude-0987654321',encoding='utf-8')
    vault=FakeVault()
    with patch.object(app,'BASE',base),patch.dict(os.environ,{k:os.environ[k] for k in ('DISPLAY','XAUTHORITY','PATH','SYSTEMROOT','SystemRoot','WINDIR','HOME','USERPROFILE','TCL_LIBRARY','TK_LIBRARY') if k in os.environ},clear=True),patch.object(app,'Settings',lambda store,base:Settings(store,base,vault,environ={})):
        window=app.App()
        window.geometry('1180x780+0+0')
        window.open_settings()
        window.update()
        window.after(500,window.quit)
        window.mainloop()
        ImageGrab.grab().crop((0,0,window.winfo_width(),window.winfo_height())).save('settings-preview.png')
        window.open_import()
        window.update()
        ImageGrab.grab().crop((0,0,window.winfo_width(),window.winfo_height())).save('import-preview.png')
        window.open_mail_settings()
        window.update()
        ImageGrab.grab().crop((0,0,window.winfo_width(),window.winfo_height())).save('accounts-preview.png')
        preview=window.show_mail_preview([
            {'id':'101','date':'15.10.2026 09:30','sender':'projekt@example.com','subject':'Freigabe zum Projektstart'},
            {'id':'102','date':'14.10.2026 16:10','sender':'team@example.com','subject':'Protokoll und offene Punkte'},
            {'id':'103','date':'13.10.2026 11:45','sender':'lieferant@example.com','subject':'Angebot Version 2'}],
            'synthetic-account','INBOX','Beispielkonto · beispiel@example.com')
        preview.geometry('1080x650+0+0')
        preview.update()
        window.toggle_preview_selected(preview,('mail-0','mail-2'))
        preview.update()
        ImageGrab.grab().crop((0,0,preview.winfo_width(),preview.winfo_height())).save('email-preview.png')
        window.close_mail_preview(preview)
        window.destroy()
