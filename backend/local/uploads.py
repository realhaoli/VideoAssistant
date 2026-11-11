"""Reject oversized upload bodies before multipart spooling consumes the disk."""
import os
import re
import tempfile
from contextlib import ExitStack

from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartParser
from starlette.responses import JSONResponse

from .resources import DiskSpaceError, reserve_disk
from .config import DATA

UPLOAD_FILES = 2
PDF_UPLOAD_LIMIT = 25 * 1024**2
FORM_FIELD_LIMIT = 32 * 1024


def upload_form(request, *, max_files, max_fields):
    content_type = request.headers.get('content-type', '').split(';', 1)[0].strip().lower()
    if content_type != 'multipart/form-data':
        raise HTTPException(415, '请使用 multipart/form-data 上传文件')
    return request.form(max_files=max_files, max_fields=max_fields, max_part_size=FORM_FIELD_LIMIT)


class UploadLimits:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('method') != 'POST':
            return await self.app(scope, receive, send)
        path = scope.get('path', '').rstrip('/')
        if path == '/api/v1/tasks/upload':
            maximum = (int(os.getenv('MAX_UPLOAD_MB', '4096')) + 17) * 1024**2
            size_error = '上传超过大小限制，请调整 MAX_UPLOAD_MB 或使用较小的素材'
            file_count = UPLOAD_FILES
        elif re.fullmatch(r'/api/v1/tasks/[^/]+/mindmap/pdf', path):
            maximum = PDF_UPLOAD_LIMIT + 1024**2
            size_error = 'PDF 文件超过 25 MB'
            file_count = 1
        else:
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        try:
            length = int(headers.get(b'content-length', b'0'))
            if length < 0 or length > maximum:
                return await JSONResponse({'detail': size_error}, status_code=413)(scope, receive, send)
        except ValueError:
            return await JSONResponse({'detail':'无效的上传长度'}, status_code=400)(scope, receive, send)
        with ExitStack() as reservations:
            try:
                spool = reservations.enter_context(reserve_disk(tempfile.gettempdir(), length))
                target = reservations.enter_context(reserve_disk(DATA, length))
            except DiskSpaceError as exc:
                return await JSONResponse({'detail':str(exc)}, status_code=507)(scope, receive, send)
            scope.setdefault('state', {})['upload_disk'] = target
            scope['state']['upload_spool'] = spool
            consumed = 0
            rollover = (file_count * MultiPartParser.spool_max_size + 2 * FORM_FIELD_LIMIT
                        if headers.get(b'content-type', b'').lower().startswith(b'multipart/form-data') else 0)

            async def limited_receive():
                nonlocal consumed
                message = await receive()
                if message['type'] == 'http.request':
                    incoming = len(message.get('body', b''))
                    new_total = consumed + incoming
                    if new_total > maximum or (length and new_total > length):
                        raise HTTPException(413, size_error)
                    # Multipart processing writes the previous chunk before requesting another.
                    # Keep room for file buffers that may still roll from memory to disk.
                    try:
                        already_spooled = max(0, consumed - rollover)
                        spool.resize((length or new_total) - already_spooled)
                        if not length:
                            target.resize(new_total)
                    except DiskSpaceError as exc:
                        raise HTTPException(507, str(exc)) from exc
                    consumed = new_total
                return message
            await self.app(scope, limited_receive, send)
