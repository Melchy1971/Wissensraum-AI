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
        window.destroy()
