"""Fault injection for long inputs, bounded resources and CPU recovery."""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import httpx
from PIL import Image
import pytest

from local import media, model_client, pdf_export
from local.intelligence import Intelligence
from local.jobs import Cancelled
from local.limits import request_budget
from local.model_client import ModelLimitError
from local.resources import FairLimiter
from local.storage import atomic_json


@pytest.mark.parametrize('supported,expected',[
    ({'int8_float16','float16','float32'},'int8_float16'),
    ({'float16','float32'},'float16'),
    ({'int8_float32','float32'},'int8_float32'),
    ({'float32'},'float32'),
])
def test_unknown_cuda_device_uses_supported_precision_not_gpu_name(tmp_path,monkeypatch,supported,expected):
    calls=[]
    monkeypatch.setenv('WHISPER_COMPUTE_TYPE','auto')
    monkeypatch.setattr(media,'MODEL',None)
    monkeypatch.setattr(media,'MODEL_KEY',None)
    monkeypatch.setattr(media,'WHISPER_RUNTIME',{})
    monkeypatch.setattr(media,'DATA',tmp_path)
    monkeypatch.setitem(sys.modules,'ctranslate2',SimpleNamespace(get_supported_compute_types=lambda device:supported))
    monkeypatch.setitem(sys.modules,'faster_whisper',SimpleNamespace(WhisperModel=lambda path,**kwargs:calls.append(kwargs) or object()))
    media._load_whisper('small','cuda')
    media._load_whisper('small','cuda')
    assert len(calls)==1 and calls[0]['compute_type']==expected
    assert calls[0]['device']=='cuda' and calls[0]['num_workers']==1
    media._load_whisper('small','cpu')
    assert calls[-1]['compute_type']=='int8' and calls[-1]['device']=='cpu'


def test_two_hour_asr_resumes_chunks_and_keeps_cpu_fallback(tmp_path, monkeypatch):
    source = tmp_path/'source.mp4'
    source.write_bytes(b'long video fixture')
    calls, phases, percentages, warnings = [], [], [], []
    monkeypatch.setattr(media, 'cuda_available', lambda:True)
    monkeypatch.setenv('WHISPER_CHUNK_SECONDS', '300')
    def ffmpeg(args, check, **kwargs):
        Path(args[-1]).write_bytes(b'audio')
    monkeypatch.setattr(media, 'ffmpeg_command', ffmpeg)
    fail = True
    def recognize(path, work, check, progress, **kwargs):
        nonlocal fail
        index = int(work.name)
        calls.append((index, kwargs['settings'].get('whisper_device', 'auto')))
        assert (work/'audio.wav').exists()
        if index == 2 and fail:
            fail = False
            raise Cancelled('interrupted')
        # Simulate the first CUDA attempt falling back to CPU. Subsequent new
        # chunks must use CPU directly, without another failing GPU attempt.
        atomic_json(work/'whisper-runtime.json', {'model':'small','device':'cpu'})
        progress(50)
        return [{'start':0,'end':kwargs['duration'],'text':f'分块{index}'}], 'zh'
    monkeypatch.setattr(media, '_transcribe', recognize)
    def run():
        return media.transcribe(source,tmp_path,lambda:None,percentages.append,
            settings={'whisper_device':'auto'},warning=warnings.append,
            phase=lambda *args:phases.append(args),duration=7200)
    with pytest.raises(Cancelled):
        run()
    assert calls == [(0,'auto'),(1,'cpu'),(2,'cpu')]
    assert len(list((tmp_path/'whisper-chunks').glob('*/*.json'))) == 2
    before = len(calls)
    cues, language = run()
    assert len(calls)-before == 22
    assert calls[before] == (2,'auto')  # A later run may use a repaired GPU.
    assert all(device=='cpu' for _,device in calls[before+1:])
    assert language=='zh' and len(cues)==24
    assert cues[0]['start']==0 and cues[-1]['end']==7200
    assert all(left['end']==right['start'] for left,right in zip(cues,cues[1:]))
    assert percentages[-1]==100
    before = len(calls)
    assert run()[0] == cues
    assert len(calls)==before
    record = next((tmp_path/'whisper-chunks').glob('*/00005.json'))
    record.write_text('{broken',encoding='utf-8')
    assert run()[0]==cues and len(calls)==before+1 and calls[-1][0]==5
    assert warnings and not list((tmp_path/'whisper-chunks').glob('*/00000/audio.wav'))


def test_gpu_initialization_failure_still_runs_on_cpu(tmp_path, monkeypatch):
    (tmp_path/'audio.wav').write_bytes(b'audio')
    devices, warnings = [], []
    monkeypatch.setattr(media,'cuda_available',lambda:True)
    def load(name,device):
        devices.append(device)
        if device=='cuda':
            raise RuntimeError('CUDA runtime unavailable')
        segment=SimpleNamespace(start=0,end=5,text='CPU 结果',words=None)
        return SimpleNamespace(transcribe=lambda *args,**kwargs:(iter([segment]),SimpleNamespace(duration=5,language='zh')))
    monkeypatch.setattr(media,'_load_whisper',load)
    result,_=media.transcribe(tmp_path/'source.mp4',tmp_path,lambda:None,lambda _:None,
        settings={'whisper_device':'cuda'},warning=warnings.append)
    assert devices==['cuda','cpu'] and result[0]['text']=='CPU 结果'
    assert json.loads((tmp_path/'whisper-runtime.json').read_text(encoding='utf-8'))['device']=='cpu'
    assert any('CPU' in message for message in warnings)


def test_silent_chunk_is_checkpointed_without_failing_voiced_chunks(tmp_path,monkeypatch):
    source=tmp_path/'source.wav';source.write_bytes(b'fixture')
    monkeypatch.setattr(media,'cuda_available',lambda:False)
    monkeypatch.setattr(media,'ffmpeg_command',lambda args,*a,**kw:Path(args[-1]).write_bytes(b'audio'))
    def load(name,device):
        def recognize(path,**kwargs):
            index=int(Path(path).parent.name)
            values=[] if index==1 else [SimpleNamespace(start=0,end=5,text='有效语音',words=None)]
            return iter(values),SimpleNamespace(duration=300,language='zh')
        return SimpleNamespace(transcribe=recognize)
    monkeypatch.setattr(media,'_load_whisper',load)
    result,_=media.transcribe(source,tmp_path,lambda:None,lambda _:None,duration=900)
    assert len(result)==2
    record=next((tmp_path/'whisper-chunks').glob('*/00001.json'))
    assert json.loads(record.read_text(encoding='utf-8'))['segments']==[]


def test_two_hour_planning_covers_last_cue_without_one_huge_request(tmp_path):
    engine=Intelligence({},lambda:None)
    source=[{'start':i*5,'end':(i+1)*5,'text':f'第{i}条技术事实与具体参数说明。'*4} for i in range(1440)]
    seen=[]
    def plan(batch,duration,options,metadata,checkpoint):
        assert sum(len(c['text']) for c in batch)<=4800
        assert batch[-1]['end']-batch[0]['start']<=480
        seen.extend(batch)
        return {'title':'完整讲座','chapters':[{'title':'技术章节','start_id':0,'start':batch[0]['start'],'end':batch[-1]['end']}]}
    engine._plan_small=plan
    result=engine.plan(source,7200,{}, {},tmp_path/'plan.json')
    assert seen==source and result['chapters'][0]['start']==0 and result['chapters'][-1]['end']==7200


def test_million_context_and_output_are_independent_and_checked_before_call(monkeypatch):
    calls=[]
    monkeypatch.setattr(model_client,'complete',lambda *args:calls.append(args[4]) or '成功')
    engine=Intelligence({'llm_context_tokens':1_000_000,'llm_max_output_tokens':4096},lambda:None)
    assert engine.chat('技术资料'*20000,max_tokens=10000)=='成功' and calls==[4096]
    with pytest.raises(ModelLimitError) as error:
        engine.chat('资'*400000)
    assert error.value.kind=='context' and len(calls)==1
    assert request_budget({'vlm_context_tokens':1_000_000,'vlm_max_output_tokens':2048},'图','规则',6000,4)==2048


@pytest.mark.parametrize('message,kind,maximum',[
    ('max_tokens must be in [1, 2048]','output_parameter',2048),
    ('maximum context length is 1000000 tokens','context',None),
    ('max_tokens must be less than or equal to 8192','output_parameter',8192),
    ('request entity too large','payload',None),
])
def test_provider_limit_classification(message,kind,maximum):
    error=model_client.limit_error(message,413 if kind=='payload' else 400)
    assert error.kind==kind and error.maximum==maximum


def test_provider_output_parameter_recovery_is_remembered(monkeypatch):
    calls=[]
    def complete(config,prompt,system,visual,maximum,check):
        calls.append(maximum)
        if maximum>2048:raise ModelLimitError('output_parameter',2048)
        return 'ok'
    monkeypatch.setattr(model_client,'complete',complete)
    engine=Intelligence({},lambda:None)
    assert engine.chat('test')=='ok' and engine.chat('again')=='ok'
    assert calls==[6000,2048,2048]


def test_cleaning_splits_rows_and_single_long_cue_without_losing_times():
    engine=Intelligence({},lambda:None)
    calls=[]
    def chat(prompt,**kwargs):
        calls.append(len(prompt))
        if kwargs.get('json_result'):
            rows=json.loads(prompt.rsplit('\n',1)[-1])
            if len(rows)>1 or len(rows[0]['text'])>100:raise ModelLimitError('context')
            return [{'id':row['id'],'text':row['text']} for row in rows]
        piece=prompt.rsplit('\n',1)[-1]
        if len(piece)>100:raise ModelLimitError('output')
        return piece
    engine.chat=chat
    source=[{'start':0,'end':7000,'text':'技术事实。'*300},{'start':7000,'end':7200,'text':'最终结果。'}]
    assert engine.transform(source,clean=True)==source and len(calls)>10


def test_multimodal_payload_reduces_images_and_preserves_original_indices(tmp_path):
    engine=Intelligence({},lambda:None)
    images=[{'path':tmp_path/f'{i}.png','time':i} for i in range(4)]
    def chat(prompt,visual,**kwargs):
        if len(visual)>1:raise ModelLimitError('payload')
        return {'rankings':[{'index':0,'score':90,'caption':'技术画面'}]}
    engine.chat=chat
    ranked=engine.select({'title':'系统','visual_query':'技术'},images,'技术正文')
    assert {r['index'] for r in ranked}==set(range(4))
    assert all(r['candidate'] is images[r['index']] for r in ranked)
    original=tmp_path/'large.png'
    Image.new('RGBA',(3200,2000),(100,150,200,127)).save(original)
    mime,payload=model_client.image_payload(original)
    with Image.open(io.BytesIO(base64.b64decode(payload))) as image:
        assert mime=='image/jpeg' and max(image.size)<=1600
        assert image.mode=='RGB' and len(base64.b64decode(payload))<=2*1024**2


def test_fifo_cancelled_waiter_cannot_block_other_jobs():
    limiter=FairLimiter(1)
    cancelled=threading.Event()
    order=[]
    def work(label):
        def check():
            if label==1 and cancelled.is_set():raise Cancelled('cancelled')
        try:
            with limiter.slot(check):order.append(label)
        except Cancelled:order.append('cancelled')
    with ThreadPoolExecutor(max_workers=2) as pool:
        with limiter.slot():
            first=pool.submit(work,1)
            deadline=time.monotonic()+3
            while len(limiter.waiters)!=1:
                assert time.monotonic()<deadline
                time.sleep(.005)
            second=pool.submit(work,2)
            while len(limiter.waiters)!=2:
                assert time.monotonic()<deadline
                time.sleep(.005)
            cancelled.set()
            first.result(timeout=3)
        second.result(timeout=3)
    assert order==['cancelled',2] and limiter.active==0 and not limiter.waiters


def test_multiuser_model_calls_respect_global_and_per_task_limits(monkeypatch):
    import local.intelligence as module
    monkeypatch.setattr(module,'MODELS',FairLimiter(2))
    lock=threading.Lock()
    active,peak,per_task=0,0,{}
    def complete(config,*args):
        nonlocal active,peak
        identity=config['id']
        with lock:
            active+=1;peak=max(peak,active)
            per_task[identity]=per_task.get(identity,0)+1
            assert active<=2 and per_task[identity]==1
        time.sleep(.03)
        with lock:
            active-=1;per_task[identity]-=1
        return 'ok'
    monkeypatch.setattr(model_client,'complete',complete)
    engines=[Intelligence({'id':i},lambda:None) for i in range(4)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures=[pool.submit(engine.chat,'资料') for _ in range(2) for engine in engines]
        assert [future.result(timeout=5) for future in futures]==['ok']*8
    assert peak==2 and active==0


def test_incomplete_download_components_are_never_reused(tmp_path,monkeypatch):
    for name in ['source.f137.mp4','source.f140.m4a']:(tmp_path/name).write_bytes(b'component')
    called=[]
    class Downloader:
        def __init__(self,opts,**kwargs):self.opts=opts
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def download(self,urls):
            called.append(urls)
            (tmp_path/'source.mp4').write_bytes(b'complete media')
    monkeypatch.setattr(media,'LinkDownloader',Downloader)
    monkeypatch.setattr(media,'probe',lambda path:{'streams':[{'codec_type':'video'},{'codec_type':'audio'}]})
    result=media.download('https://example.test',tmp_path,best=True,audio=False,check=lambda:None,progress=lambda _:None)
    assert called and result.name=='source.mp4'
    assert json.loads((tmp_path/'download-complete.json').read_text(encoding='utf-8'))['best']


def test_upload_guard_rejects_header_and_chunked_body_and_disk_before_processing(monkeypatch):
    from fastapi import FastAPI, Request
    import local.uploads as uploads
    import local.resources as resources
    monkeypatch.setenv('MAX_UPLOAD_MB','1')
    monkeypatch.setattr(resources, '_check_disk', lambda *args:None)
    app=FastAPI()
    app.add_middleware(uploads.UploadLimits)
    @app.post('/api/v1/tasks/upload')
    async def consume(request:Request):
        async for chunk in request.stream():pass
        return {'ok':True}
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            response=await client.post('/api/v1/tasks/upload',content=b'',headers={'Content-Length':str(19*1024**2)})
            assert response.status_code==413
            async def chunks():
                for _ in range(19):yield b'x'*1024**2
            assert (await client.post('/api/v1/tasks/upload',content=chunks())).status_code==413
            def full(*args):raise resources.DiskSpaceError('磁盘空间不足')
            monkeypatch.setattr(resources, '_check_disk', full)
            assert (await client.post('/api/v1/tasks/upload',content=b'fixture')).status_code==507
    asyncio.run(exercise())


@pytest.mark.skipif(os.getenv('RUN_PDF_RENDER_TEST')!='1',reason='requires installed Chromium and frontend build')
def test_large_mindmap_is_split_into_complete_pdf_pages(tmp_path):
    tree={'data':{'text':'两小时技术讲座'},'children':[
        {'data':{'text':f'第{i+1}章技术结论'},'children':[{'data':{'text':'关键规格与技术机制'}}]} for i in range(13)]}
    atomic_json(tmp_path/'mindmap.json',tree)
    (tmp_path/'output').mkdir()
    progress = []
    pdf_export.render(tmp_path, lambda:None, progress=progress.append)
    assert progress[-1] == 100 and any(0 < value < 100 for value in progress)
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run(['node','--input-type=module','-e',
        "import fs from 'node:fs'; import {PDFDocument} from 'pdf-lib'; const pdf=await PDFDocument.load(fs.readFileSync(process.argv[1])); process.stdout.write(String(pdf.getPageCount()));",
        str(tmp_path/'output/mindmap.pdf')],cwd=root/'frontend',capture_output=True,text=True,check=True)
    assert result.stdout=='13'
