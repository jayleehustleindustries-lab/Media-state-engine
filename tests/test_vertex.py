"""Unit tests for app/services/vertex.py.

httpx is mocked at the transport boundary (same pattern as
tests/test_storage_and_assets.py's elevenlabs test — a FakeClient patched
onto app.services.vertex.httpx.AsyncClient). Auth token minting is mocked
via vertex._access_token for tests that only care about the wire format;
one test (test_load_credentials_fails_without_any_configured_auth) exercises
the REAL google-auth ADC lookup with no mocking, since this sandbox
genuinely has no Google credentials configured — confirming the
not-configured error path end to end rather than assuming it.
"""
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.services import vertex


def _configure(monkeypatch, **overrides):
    monkeypatch.setattr(settings, 'vertex_project_id', overrides.get('project_id', 'proj-1'))
    monkeypatch.setattr(settings, 'vertex_location', overrides.get('location', 'us-central1'))
    monkeypatch.setattr(settings, 'vertex_model_id', overrides.get('model_id', 'veo-3.0-generate-001'))


class FakeResponse:
    def __init__(self, *, status_code=200, json_body=None, content=b'{}'):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.content = content or b'{}'

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._json


class FakeClient:
    def __init__(self, response, *, capture=None):
        self._response = response
        self._capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def post(self, url, headers=None, json=None):
        if self._capture is not None:
            self._capture['url'] = url
            self._capture['headers'] = headers
            self._capture['json'] = json
        return self._response


def test_not_configured_without_project_id(monkeypatch):
    monkeypatch.setattr(settings, 'vertex_project_id', '')
    with pytest.raises(vertex.VertexNotConfigured):
        vertex._require_config()


def test_load_credentials_fails_without_any_configured_auth(monkeypatch):
    """Real (unmocked) google-auth ADC lookup — this sandbox has no Google
    credentials configured, so this exercises the actual failure path."""
    monkeypatch.setattr(settings, 'vertex_service_account_json', '')
    monkeypatch.setattr(settings, 'google_application_credentials', '')
    with pytest.raises(vertex.VertexNotConfigured):
        vertex._load_credentials()


def test_reference_images_payload_converts_and_caps_at_six():
    refs = [{'data': f'b64-{i}', 'mime_type': 'image/jpeg'} for i in range(9)]
    payload = vertex._reference_images_payload(refs)
    assert len(payload) == 6
    assert payload[0] == {
        'image': {'bytesBase64Encoded': 'b64-0', 'mimeType': 'image/jpeg'},
        'referenceType': 'ASSET',
    }


def test_reference_images_payload_encodes_raw_bytes():
    payload = vertex._reference_images_payload([{'data': b'\x00\x01raw', 'mime_type': 'image/png'}])
    assert payload[0]['image']['mimeType'] == 'image/png'
    import base64
    assert base64.b64decode(payload[0]['image']['bytesBase64Encoded']) == b'\x00\x01raw'


def test_reference_images_payload_none_when_empty():
    assert vertex._reference_images_payload(None) is None
    assert vertex._reference_images_payload([]) is None


@pytest.mark.asyncio
async def test_start_clip_generation_builds_predict_long_running_request(monkeypatch):
    _configure(monkeypatch)
    capture: dict = {}
    fake_response = FakeResponse(json_body={
        'name': 'projects/proj-1/locations/us-central1/publishers/google/models/veo-3.0-generate-001/operations/op-123',
        'done': False,
    })
    with patch('app.services.vertex._access_token', AsyncMock(return_value='fake-token')), \
         patch('app.services.vertex.httpx.AsyncClient', return_value=FakeClient(fake_response, capture=capture)):
        op = await vertex.start_clip_generation(
            job_id='job-1', prompt='hook line', clip_index=0,
            reference_images=[{'data': 'abc', 'mime_type': 'image/png'}],
        )

    assert op.operation_name.endswith('operations/op-123')
    assert op.done is False
    # Confirmed wire shape (see vertex.py PROVENANCE): predictLongRunning path,
    # instances[0].prompt + referenceImages, parameters.aspectRatio/durationSeconds.
    assert capture['url'].endswith(
        'projects/proj-1/locations/us-central1/publishers/google/models/veo-3.0-generate-001:predictLongRunning'
    )
    assert capture['headers']['Authorization'] == 'Bearer fake-token'
    body = capture['json']
    assert body['instances'][0]['prompt'] == 'hook line'
    assert body['instances'][0]['referenceImages'][0]['referenceType'] == 'ASSET'
    assert body['parameters']['aspectRatio'] == '9:16'
    assert body['parameters']['durationSeconds'] == 8


@pytest.mark.asyncio
async def test_start_clip_generation_requires_prompt(monkeypatch):
    _configure(monkeypatch)
    with patch('app.services.vertex._access_token', AsyncMock(return_value='t')):
        with pytest.raises(vertex.VertexRequestError):
            await vertex.start_clip_generation(job_id='j', prompt='   ', clip_index=1)


@pytest.mark.asyncio
async def test_start_clip_generation_missing_operation_name_raises(monkeypatch):
    _configure(monkeypatch)
    fake_response = FakeResponse(json_body={'done': False})
    with patch('app.services.vertex._access_token', AsyncMock(return_value='t')), \
         patch('app.services.vertex.httpx.AsyncClient', return_value=FakeClient(fake_response)):
        with pytest.raises(vertex.VertexRequestError):
            await vertex.start_clip_generation(job_id='j', prompt='hi', clip_index=0)


@pytest.mark.asyncio
async def test_get_clip_operation_parses_completed_response_with_video_bytes(monkeypatch):
    _configure(monkeypatch)
    fake_response = FakeResponse(json_body={
        'done': True,
        'response': {'videos': [{'bytesBase64Encoded': 'ZmFrZQ==', 'mimeType': 'video/mp4'}]},
    })
    with patch('app.services.vertex._access_token', AsyncMock(return_value='t')), \
         patch('app.services.vertex.httpx.AsyncClient', return_value=FakeClient(fake_response)):
        op = await vertex.get_clip_operation('projects/p/locations/l/.../operations/op-1')
    assert op.done is True
    assert op.video_bytes_b64 == 'ZmFrZQ=='
    assert op.mime_type == 'video/mp4'
    assert op.error is None


@pytest.mark.asyncio
async def test_get_clip_operation_not_done_yet(monkeypatch):
    _configure(monkeypatch)
    fake_response = FakeResponse(json_body={'done': False})
    with patch('app.services.vertex._access_token', AsyncMock(return_value='t')), \
         patch('app.services.vertex.httpx.AsyncClient', return_value=FakeClient(fake_response)):
        op = await vertex.get_clip_operation('op-1')
    assert op.done is False
    assert op.video_bytes_b64 is None


@pytest.mark.asyncio
async def test_get_clip_operation_surfaces_provider_error(monkeypatch):
    _configure(monkeypatch)
    fake_response = FakeResponse(json_body={
        'done': True,
        'error': {'message': 'generation blocked by safety filter'},
    })
    with patch('app.services.vertex._access_token', AsyncMock(return_value='t')), \
         patch('app.services.vertex.httpx.AsyncClient', return_value=FakeClient(fake_response)):
        op = await vertex.get_clip_operation('op-1')
    assert op.done is True
    assert op.error == 'generation blocked by safety filter'


@pytest.mark.asyncio
async def test_get_clip_operation_no_videos_is_flagged_as_error(monkeypatch):
    _configure(monkeypatch)
    fake_response = FakeResponse(json_body={'done': True, 'response': {'videos': []}})
    with patch('app.services.vertex._access_token', AsyncMock(return_value='t')), \
         patch('app.services.vertex.httpx.AsyncClient', return_value=FakeClient(fake_response)):
        op = await vertex.get_clip_operation('op-1')
    assert op.done is True
    assert op.error


@pytest.mark.asyncio
async def test_http_401_raises_authentication_error(monkeypatch):
    _configure(monkeypatch)
    fake_response = FakeResponse(status_code=401, json_body={'error': {'message': 'bad token'}})
    with patch('app.services.vertex._access_token', AsyncMock(return_value='t')), \
         patch('app.services.vertex.httpx.AsyncClient', return_value=FakeClient(fake_response)):
        with pytest.raises(vertex.VertexAuthenticationError):
            await vertex.start_clip_generation(job_id='j', prompt='hi', clip_index=0)


@pytest.mark.asyncio
async def test_http_500_raises_request_error_with_provider_message(monkeypatch):
    _configure(monkeypatch)
    fake_response = FakeResponse(status_code=500, json_body={'error': {'message': 'internal error'}})
    with patch('app.services.vertex._access_token', AsyncMock(return_value='t')), \
         patch('app.services.vertex.httpx.AsyncClient', return_value=FakeClient(fake_response)):
        with pytest.raises(vertex.VertexRequestError) as exc_info:
            await vertex.get_clip_operation('op-1')
    assert exc_info.value.status_code == 500
    assert 'internal error' in str(exc_info.value)
