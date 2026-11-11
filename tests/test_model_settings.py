"""Symmetric model configuration filling, with secrets kept on the server."""
import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI

from local import config


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    for name in ('LLM_API_FORMAT','LLM_BASE_URL','MODEL_ID','LLM_API_KEY','VLM_API_FORMAT','VLM_BASE_URL','VLM_MODEL_ID','VLM_API_KEY'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, 'DATA', tmp_path)
    monkeypatch.setattr(config, 'SETTINGS', tmp_path/'settings.json')
    return tmp_path/'settings.json'


@pytest.mark.parametrize('source,target', [('llm','vlm'), ('vlm','llm')])
def test_single_model_configuration_fills_the_other_side(isolated_settings, source, target):
    values = {source+'_api_format':'anthropic', source+'_base_url':'https://models.example.test/v1', source+'_model':'vision-test',source+'_api_key':'fixture-secret'}
    isolated_settings.write_text(json.dumps(values), encoding="utf-8")
    before = isolated_settings.read_bytes()
    filled = config.load()
    assert all(filled[target+'_'+name] == filled[source+'_'+name] for name in ('api_format','base_url','model','api_key'))
    public = config.public()
    assert public['llm_key_configured'] and public['vlm_key_configured']
    assert public['llm_key_mask'] == public['vlm_key_mask'] == '*' * len('fixture-secret')
    assert 'fixture-secret' not in json.dumps(public)
    assert isolated_settings.read_bytes() == before  # Opening settings is read-only.


def test_existing_independent_models_are_preserved(isolated_settings):
    values = {'llm_api_format':'openai', 'llm_base_url':'https://llm.example.test/v1','llm_model':'text-test','llm_api_key':'text-secret',
              'vlm_api_format':'anthropic','vlm_base_url':'https://vlm.example.test/v1','vlm_model':'vision-test','vlm_api_key':'vision-secret'}
    isolated_settings.write_text(json.dumps(values), encoding="utf-8")
    loaded = config.load()
    assert all(loaded[key] == value for key,value in values.items())
    public = config.public()
    assert public['llm_key_mask'] == '*' * len('text-secret')
    assert public['vlm_key_mask'] == '*' * len('vision-secret')
    assert all(secret not in json.dumps(public) for secret in ('text-secret', 'vision-secret'))
    config.save({'llm_api_key':'','vlm_api_key':'','whisper_model':'base'})
    saved = json.loads(isolated_settings.read_text(encoding="utf-8"))
    assert all(saved[key] == value for key,value in values.items())


def test_partially_configured_model_keeps_its_own_nonempty_fields(isolated_settings):
    isolated_settings.write_text(json.dumps({'llm_api_format':'anthropic','llm_base_url':'https://models.example.test/v1','llm_model':'text-model','llm_api_key':'fixture-secret',
                                            'vlm_api_format':'openai','vlm_model':'custom-vision'}), encoding="utf-8")
    values = config.load()
    assert values['vlm_model'] == 'custom-vision' and values['vlm_api_format'] == 'openai'
    assert values['vlm_base_url'] == values['llm_base_url'] and values['vlm_api_key'] == values['llm_api_key']


def test_settings_api_accepts_vlm_only_and_persists_both_without_exposing_keys(isolated_settings):
    from routers.tasks import router
    app = FastAPI();app.include_router(router)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            response = await client.put('/api/v1/settings',json={'llm_base_url':'','llm_model':'','vlm_api_format':'anthropic','vlm_base_url':'https://models.example.test/v1','vlm_model':'vision-test','vlm_api_key':'fixture-secret'})
            assert response.status_code == 200
            public = response.json()
            assert public['llm_api_format'] == public['vlm_api_format'] == 'anthropic'
            assert public['llm_model'] == public['vlm_model'] == 'vision-test'
            assert public['llm_key_configured'] and public['vlm_key_configured']
            assert public['llm_key_mask'] == public['vlm_key_mask'] == '*' * len('fixture-secret')
            assert not any(key.endswith('_api_key') for key in public)
            assert 'fixture-secret' not in response.text
            assert (await client.get('/api/v1/settings')).json()['llm_model'] == 'vision-test'
    asyncio.run(exercise())
    saved = json.loads(isolated_settings.read_text(encoding="utf-8"))
    assert saved['llm_api_key'] == saved['vlm_api_key'] == 'fixture-secret'


def test_unconfigured_models_have_explicit_formats_but_no_phantom_credentials(isolated_settings):
    values = config.public()
    assert values['llm_api_format'] == values['vlm_api_format'] == 'openai'
    assert not values['llm_model'] and not values['vlm_model']
    assert not values['llm_key_configured'] and not values['vlm_key_configured']
    assert values['llm_key_mask'] == values['vlm_key_mask'] == ''


def test_masked_settings_can_replace_a_key_and_keep_it_when_left_empty(isolated_settings):
    from routers.tasks import router
    config.save({'llm_base_url':'https://models.example.test/v1','llm_model':'fixture-model','llm_api_key':'original-fixture-key'})
    app = FastAPI(); app.include_router(router)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            visible = (await client.get('/api/v1/settings')).json()
            assert visible['llm_key_mask'] == '*' * len('original-fixture-key')
            response = await client.put('/api/v1/settings',json={**visible,'llm_api_key':'replacement-fixture-key'})
            assert response.status_code == 200
            assert response.json()['llm_key_mask'] == '*' * len('replacement-fixture-key')
            assert response.json()['vlm_key_mask'] == '*' * len('original-fixture-key')
            assert 'replacement-fixture-key' not in response.text
            retained = await client.put('/api/v1/settings',json={**response.json(),'llm_api_key':'','vlm_api_key':''})
            assert retained.status_code == 200
            assert retained.json()['llm_key_mask'] == response.json()['llm_key_mask']
    asyncio.run(exercise())
    saved = json.loads(isolated_settings.read_text(encoding="utf-8"))
    assert saved['llm_api_key'] == 'replacement-fixture-key'
    assert saved['vlm_api_key'] == 'original-fixture-key'


def test_subtitle_profile_is_independent_and_tested_by_settings_api(isolated_settings, monkeypatch):
    from local import model_client
    from routers.tasks import router

    config.save({'llm_base_url':'https://reports.example.test/v1', 'llm_model':'report-model',
                 'llm_api_key':'report-fixture-key', 'llm_context_tokens':1000000})
    calls = []

    def complete(values, prompt, system, visual, maximum, check):
        calls.append(model_client.connection(values, bool(visual)))
        return '连接成功'

    monkeypatch.setattr(model_client, 'complete', complete)
    app = FastAPI()
    app.include_router(router)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            profile = {'use_subtitle_model':True, 'subtitle_model':'subtitle-model',
                       'subtitle_api_format':'anthropic', 'subtitle_base_url':'https://subtitles.example.test/v1',
                       'subtitle_context_tokens':24000, 'subtitle_max_output_tokens':4096, 'asr_cleanup_mode':'model'}
            incomplete = await client.put('/api/v1/settings', json=profile)
            assert incomplete.status_code == 400
            saved = await client.put('/api/v1/settings', json={**profile, 'subtitle_api_key':'subtitle-fixture-key'})
            assert saved.status_code == 200
            assert saved.json()['subtitle_key_configured'] and 'subtitle-fixture-key' not in saved.text
            assert saved.json()['llm_context_tokens'] == 1000000
            legacy = await client.put('/api/v1/settings', json={'whisper_model':'base', 'subtitle_api_key':''})
            assert legacy.status_code == 200 and legacy.json()['use_subtitle_model']
            tested = await client.post('/api/v1/settings/test')
            assert tested.status_code == 200 and tested.json()['message'] == '大语言模型、字幕模型连接成功'

    asyncio.run(exercise())
    assert [(call['model'], call['format'], call['key']) for call in calls] == [
        ('report-model', 'openai', 'report-fixture-key'), ('subtitle-model', 'anthropic', 'subtitle-fixture-key'),
    ]
    values = config.load()
    subtitle = config.subtitle_model_settings(values)
    assert subtitle['llm_context_tokens'] == 24000 and subtitle['llm_max_output_tokens'] == 4096
    assert config.subtitle_model_settings({**values, 'use_subtitle_model':False})['llm_model'] == 'report-model'


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
def test_capacity_discovery_only_reads_explicit_fields_for_selected_model(monkeypatch, protocol):
    from local import model_client

    requests = []

    def metadata(url, headers):
        requests.append((url, headers))
        if protocol == 'anthropic':
            return {'id':'vendor/model', 'max_input_tokens':200000, 'max_tokens':64000}
        if url.endswith('/models'):
            return {'data': [
                {'id':'other', 'context_length':2000000, 'max_output_tokens':131072},
                {'id':'vendor/model', 'context_length':1000000, 'top_provider':{'max_completion_tokens':32768}},
            ]}
        return {'id':'vendor/model', 'owned_by':'vendor'}

    monkeypatch.setattr(model_client, '_read_model_metadata', metadata)
    values = {'llm_api_format':protocol, 'llm_api_key':'fixture', 'llm_model':'vendor/model',
              'llm_base_url':'https://models.example.test/v1'}
    limits = model_client.discover_model_limits(values)
    assert requests[0][0].endswith('/models/vendor%2Fmodel')
    if protocol == 'anthropic':
        assert requests[0][1]['x-api-key'] == 'fixture'
        assert limits == {'context_tokens':200000, 'max_output_tokens':64000}
    else:
        assert requests[0][1]['Authorization'] == 'Bearer fixture'
        assert len(requests) == 2 and limits == {'context_tokens':1000000, 'max_output_tokens':32768}


def test_test_model_updates_only_its_advertised_capacity_after_success(isolated_settings, monkeypatch):
    from local import model_client
    from routers.tasks import router

    config.save({'llm_base_url':'https://reports.example.test/v1', 'llm_model':'llm-fixture', 'llm_api_key':'fixture-llm',
                 'vlm_base_url':'https://vision.example.test/v1', 'vlm_model':'vlm-fixture', 'vlm_api_key':'fixture-vlm',
                 'use_subtitle_model':True, 'subtitle_base_url':'https://subtitle.example.test/v1',
                 'subtitle_model':'subtitle-fixture', 'subtitle_api_key':'fixture-subtitle'})
    tested = []

    def metadata(url, headers):
        if 'vlm-fixture' in url:
            return {'id':'vlm-fixture', 'context_length':1000000, 'max_output_tokens':16384}
        if 'subtitle-fixture' in url:
            return {'id':'subtitle-fixture', 'max_output_tokens':4096}
        return None

    def complete(values, prompt, system, visual, maximum, check):
        selected = model_client.connection(values, bool(visual))
        if visual:
            assert len(visual) == 1 and visual[0].is_file()
        tested.append((selected['model'], bool(visual)))
        if selected['model'] == 'subtitle-fixture':
            raise RuntimeError('fixture connection error')
        return '成功'

    monkeypatch.setattr(model_client, '_read_model_metadata', metadata)
    monkeypatch.setattr(model_client, 'complete', complete)
    app = FastAPI()
    app.include_router(router)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            api = '/api/v1/settings/test'
            vision = await client.post(api, json={'role':'vlm'})
            assert vision.status_code == 200
            assert vision.json()['detected'] == {'vlm_context_tokens':1000000, 'vlm_max_output_tokens':16384}
            subtitle = await client.post(api, json={'role':'subtitle'})
            assert subtitle.status_code == 502 and '字幕模型测试失败' in subtitle.json()['detail']
            language = await client.post(api, json={'role':'llm'})
            assert language.status_code == 200 and language.json()['detected'] == {}

    asyncio.run(exercise())
    assert tested == [('vlm-fixture', True), ('subtitle-fixture', False), ('llm-fixture', False)]
    assert config.load()['subtitle_max_output_tokens'] == 8192
    assert config.load()['llm_context_tokens'] == 32768
    assert config.load()['vlm_context_tokens'] == 1000000


@pytest.mark.parametrize("provider", ["ark", "bailian"])
def test_discovery_reads_provider_capacity_without_application_clamping(monkeypatch, provider):
    from local import model_client

    base = ("https://ark.cn-beijing.volces.com/api/v3/chat/completions" if provider == "ark"
            else "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1")

    def metadata(url, headers):
        assert headers == {"Authorization": "Bearer fixture"}
        if url == "https://ark.cn-beijing.volces.com/api/v3/models":
            return {"data": [{"id": "fixture", "token_limits": {
                "context_window": 1048576, "max_output_token_length": 262144,
            }}]}
        if url == "https://workspace.cn-beijing.maas.aliyuncs.com/api/v1/models?model=fixture":
            return {"output": {"models": [{"model": "fixture", "model_info": {
                "context_window": 1048576, "max_input_tokens": 1040000, "max_output_tokens": 262144,
            }}]}}
        return None

    monkeypatch.setattr(model_client, "_read_model_metadata", metadata)
    assert model_client.discover_model_limits({
        "llm_api_format": "openai", "llm_api_key": "fixture", "llm_model": "fixture", "llm_base_url": base,
    }) == {"context_tokens": 1048576, "max_output_tokens": 262144}


def test_draft_connection_and_save_discover_limits_for_the_selected_profile(isolated_settings, monkeypatch):
    from local import model_client
    from routers.tasks import router

    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "8192")
    config.save({"llm_model": "old", "llm_base_url": "https://old.example.test/v1",
                 "llm_api_key": "old-secret", "llm_max_output_tokens": 64000})
    before = isolated_settings.read_bytes()
    calls = []

    def discover(values, **_):
        selected = model_client.connection(values)
        calls.append((selected["model"], selected["key"]))
        return {"context_tokens": 1048576}

    monkeypatch.setattr(model_client, "discover_model_limits", discover)
    monkeypatch.setattr(model_client, "complete", lambda *args: "连接成功")
    app = FastAPI()
    app.include_router(router)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            draft = {"llm_model": "new", "llm_base_url": "https://new.example.test/v1"}
            missing_key = await client.post("/api/v1/settings/test", json={"role": "llm", "draft": draft})
            assert missing_key.status_code == 400 and calls == []
            draft["llm_api_key"] = "new-secret"
            test = await client.post("/api/v1/settings/test", json={"role": "llm", "draft": draft})
            assert test.status_code == 200
            assert test.json()["capacity"] == {"llm_context_tokens": 1048576, "llm_max_output_tokens": 8192}
            assert isolated_settings.read_bytes() == before
            saved = await client.put("/api/v1/settings?detect_for=llm", json=draft)
            assert saved.status_code == 200 and saved.json()["model_check"]["ok"]
            assert saved.json()["llm_context_tokens"] == 1048576 and saved.json()["llm_max_output_tokens"] == 8192
            assert saved.json()["vlm_model"] == "old"
            assert "new-secret" not in saved.text

    asyncio.run(exercise())
    assert calls == [("new", "new-secret"), ("new", "new-secret")]


def test_global_save_updates_all_capacities_without_running_other_connection_tests(isolated_settings, monkeypatch):
    from local import model_client
    from routers.tasks import router

    config.save({"use_vlm": False, "llm_model": "original", "llm_base_url": "https://models.example.test/v1",
                 "llm_api_key": "fixture-key"})
    discovery, inference = [], []

    def discover(values, *, visual):
        model = model_client.connection(values, visual)["model"]
        discovery.append((model, visual))
        return {"context_tokens": 1048576, "max_output_tokens": 128000}

    def complete(values, prompt, system, visual, maximum, check):
        inference.append((model_client.connection(values, bool(visual))["model"], bool(visual)))
        return "连接成功"

    monkeypatch.setattr(model_client, "discover_model_limits", discover)
    monkeypatch.setattr(model_client, "complete", complete)
    app = FastAPI()
    app.include_router(router)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            draft = {"llm_model": "text-model", "vlm_model": "vision-model", "use_vlm": True,
                     "subtitle_model": "subtitle-model", "use_subtitle_model": True,
                     "subtitle_base_url": "https://subtitle.example.test/v1", "subtitle_api_key": "subtitle-key",
                     "asr_cleanup_mode": "model", "whisper_model": "medium", "whisper_device": "cpu"}
            result = await client.post("/api/v1/settings/test", json={"role": "vlm", "draft": draft})
            assert result.status_code == 200 and result.json()["role"] == "vlm"
            assert inference == [("vision-model", True)] and discovery == inference
            assert not config.load()["use_vlm"]
            discovery.clear()

            saved = await client.put("/api/v1/settings?detect_for=all", json=draft)
            assert saved.status_code == 200
            data = saved.json()
            assert set(data["detected_capacities"]) == {"llm", "vlm", "subtitle"}
            assert data["llm_context_tokens"] == data["vlm_context_tokens"] == data["subtitle_context_tokens"] == 1048576
            assert data["use_vlm"] and data["use_subtitle_model"] and data["asr_cleanup_mode"] == "model"
            assert data["whisper_model"] == "medium" and data["whisper_device"] == "cpu"
            assert set(discovery) == {("text-model", False), ("vision-model", True), ("subtitle-model", False)}
            assert inference == [("vision-model", True)]

    asyncio.run(exercise())
