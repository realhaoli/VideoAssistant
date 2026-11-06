"""Reject oversized upload bodies before multipart spooling consumes the disk."""
import os
import tempfile

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from .resources import ensure_disk
from .config import DATA


class UploadLimits:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('method') != 'POST' or scope.get('path') != '/api/v1/tasks/upload':
            return await self.app(scope, receive, send)
        maximum = int(os.getenv('MAX_UPLOAD_MB', '4096')) * 1024**2 + 17 * 1024**2
        headers = dict(scope.get('headers', []))
        try:
            length = int(headers.get(b'content-length', b'0'))
            if length < 0 or length > maximum:
                return await JSONResponse({'detail':'上传超过大小限制，请调整 MAX_UPLOAD_MB 或使用较小的素材'}, status_code=413)(scope, receive, send)
        except ValueError:
            return await JSONResponse({'detail':'无效的上传长度'}, status_code=400)(scope, receive, send)
        try:
            ensure_disk(DATA, length * 2)
            ensure_disk(tempfile.gettempdir(), length)
        except ValueError as exc:
            return await JSONResponse({'detail':str(exc)}, status_code=507)(scope, receive, send)
        consumed = 0
        async def limited_receive():
            nonlocal consumed
            message = await receive()
            if message['type'] == 'http.request':
                consumed += len(message.get('body', b''))
                if consumed > maximum:
                    raise HTTPException(413, '上传超过大小限制，已停止接收')
                try:
                    ensure_disk(DATA, len(message.get('body', b'')) * 2)
                    ensure_disk(tempfile.gettempdir(), len(message.get('body', b'')))
                except ValueError as exc:
                    raise HTTPException(507, str(exc)) from exc
            return message
        await self.app(scope, limited_receive, send)
