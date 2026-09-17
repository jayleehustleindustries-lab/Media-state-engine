import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch
import pytest

from app.services import storage
from app.config import settings


def test_storage_persists_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'asset_storage_dir', str(tmp_path))
    path = storage.save_bytes('audio/job1.mp3', b'ID3fakeaudio')
    assert path == 'audio/job1.mp3'
    assert storage.exists(path)
    assert storage.read_bytes(path) == b'ID3fakeaudio'


@pytest.mark.asyncio
async def test_elevenlabs_persists_audio(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'asset_storage_dir', str(tmp_path))
    monkeypatch.setattr(settings, 'elevenlabs_api_key', 'k')
    monkeypatch.setattr(settings, 'elevenlabs_voice_id', 'v')

    class FakeResp:
        status_code = 200
        content = b'\xff\xfb audio-bytes'
        headers = {'location': 'https://cdn/audio.mp3'}

        def raise_for_status(self):
            return None

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, *a, **k):
            return FakeResp()

    with patch('app.services.elevenlabs.httpx.AsyncClient', return_value=FakeClient()):
        from app.services import elevenlabs
        result = await elevenlabs.generate('hello world', 'job-abc')
    assert result['storage_path'] == 'audio/job-abc.mp3'
    assert result['meta']['persisted'] is True
    assert (tmp_path / 'audio/job-abc.mp3').read_bytes() == b'\xff\xfb audio-bytes'
