"""Background PDF export using the same bundled renderer as the web preview."""
import os
import sys
import tempfile
from pathlib import Path

from . import media
from .storage import path_lock
from .resources import PDF, ensure_disk, reserve_disk, setting



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
                budget = 32 * 1024**2
                with reserve_disk(target.parent, budget) as disk:
                    def check_output():
                        check()
                        disk.resize(max(4 * 1024**2, budget - temporary.stat().st_size))
                        ensure_disk(target.parent)

                    media.command([sys.executable, str(script), str(source), str(temporary)], check_output,
                                  timeout=setting('PDF_TIMEOUT_SECONDS', 300, low=60, high=600) + 30)
                    with temporary.open('rb') as content:
                        signature = content.read(5)
                        content.seek(max(0, temporary.stat().st_size - 1024))
                        if signature != b'%PDF-' or b'%%EOF' not in content.read(1024):
                            raise RuntimeError('导图 PDF 渲染结果无效')
                    temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
