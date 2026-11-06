"""Video Assistant local web application. No database, OSS or remote ASR."""
import os
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / 'variables.env')

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from local.jobs import store
from local.config import DATA
from routers.tasks import router

app = FastAPI(title='Video Assistant · 中文讲解汇报', version='2.0.0')
app.add_middleware(CORSMiddleware, allow_origins=['http://localhost:5173', 'http://127.0.0.1:5173'], allow_origin_regex=r'http://(?:10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+):5173', allow_credentials=False, allow_methods=['*'], allow_headers=['*'])
app.include_router(router)

@app.on_event('startup')
def startup():
    store.start()

@app.on_event('shutdown')
def shutdown():
    store.stop()

@app.get('/health')
def health():
    return {'success': True, 'data': {'status': 'healthy', 'storage': 'local', 'timestamp': int(time.time())}}

DIST = Path(__file__).resolve().parents[1] / 'frontend' / 'dist'
if DIST.exists():
    app.mount('/assets', StaticFiles(directory=DIST / 'assets'), name='assets')

@app.get('/')
def index():
    if (DIST / 'index.html').exists():
        return FileResponse(DIST / 'index.html')
    return {'message': '请先构建前端，或使用 http://localhost:5173 开发服务'}

if __name__ == '__main__':
    import uvicorn
    uvicorn.run('app:app', host=os.getenv('HOST', '0.0.0.0'), port=8080)
