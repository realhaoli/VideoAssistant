"""Background PDF export using the same bundled renderer as the web preview."""
import os
import sys
import tempfile
from pathlib import Path

from . import media
from .storage import path_lock
from .resources import PDF, setting



def render(folder, check):
    folder = Path(folder)
    source = folder / 'mindmap.json'
    target = folder / 'output' / 'mindmap.pdf'
    with PDF.slot(check):
        with path_lock(target.parent):
            check()
            if target.is_file():
                return
            fd, temporary = tempfile.mkstemp(prefix='.mindmap-', suffix='.pdf', dir=target.parent)
            os.close(fd)
            temporary = Path(temporary)
            try:
                script = Path(__file__).with_name('render_pdf.py')
                media.command([sys.executable, str(script), str(source), str(temporary)], check,
                              timeout=setting('PDF_TIMEOUT_SECONDS', 300, low=60, high=600) + 30)
                content = temporary.read_bytes()
                if not content.startswith(b'%PDF-') or b'%%EOF' not in content[-1024:]:
                    raise RuntimeError('导图 PDF 渲染结果无效')
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
