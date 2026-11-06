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
