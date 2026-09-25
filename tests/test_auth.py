import os
import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

from app.auth import is_public_path, ApiKeyMiddleware


def test_health_is_public_path():
    assert is_public_path('/health')
    assert is_public_path('/webhooks/heygen')
    assert not is_public_path('/jobs')
    assert not is_public_path('/jobs/x/reconcile')


@pytest.fixture
def client():
    with patch('app.main.connect', new_callable=AsyncMock), patch('app.main.close', new_callable=AsyncMock):
        from app.main import app
        with TestClient(app) as c:
            yield c


def test_health_without_api_key(client):
    r = client.get('/health')
    assert r.status_code == 200
    assert r.json()['status'] == 'ok'


def test_avatar_intake_page_is_public_but_has_no_provider_key(client):
    r = client.get('/avatar')
    assert r.status_code == 200
    assert 'Avatar source vault' in r.text
    assert 'HeyGen API key' not in r.text


def test_avatar_preflight_requires_engine_access_key(client):
    r = client.post('/avatars/photo/preflight', json={})
    assert r.status_code == 401


def test_avatar_preflight_returns_non_mutating_quality_result(client):
    key = os.environ['MEDIA_ENGINE_API_KEY']
    result = {
        'passed': True,
        'blockers': [],
        'advisories': [],
        'manual_checks': ['inspect the face'],
        'photo': {'width': 1600, 'height': 2000},
    }
    with patch('app.api.avatar.avatar_intake.preflight_photo_url', new_callable=AsyncMock) as mock_preflight:
        mock_preflight.return_value = (object(), result)
        r = client.post(
            '/avatars/photo/preflight',
            json={
                'name': 'Jordan — Studio Anchor',
                'primary_photo_url': 'https://cdn.example.com/portrait.jpg',
                'confirmed_likeness_rights': True,
            },
            headers={'X-API-Key': key},
        )
    assert r.status_code == 200
    assert r.json()['can_create'] is True
    assert r.json()['provider_action'] == 'none'
    mock_preflight.assert_awaited_once()


def test_avatar_creation_blocks_low_quality_source_before_provider(client):
    key = os.environ['MEDIA_ENGINE_API_KEY']
    result = {'passed': False, 'blockers': ['Short edge is 600px'], 'advisories': [], 'manual_checks': [], 'photo': {}}
    with patch('app.api.avatar.avatar_intake.preflight_photo_url', new_callable=AsyncMock) as mock_preflight, patch(
        'app.api.avatar.avatar_intake.create_photo_avatar', new_callable=AsyncMock
    ) as mock_create:
        mock_preflight.return_value = (object(), result)
        r = client.post(
            '/avatars/photo',
            json={
                'name': 'Jordan — Studio Anchor',
                'primary_photo_url': 'https://cdn.example.com/low-res.jpg',
                'confirmed_likeness_rights': True,
            },
            headers={'X-API-Key': key},
        )
    assert r.status_code == 422
    assert 'strict fidelity gate' in r.json()['detail']
    mock_create.assert_not_awaited()


def test_jobs_requires_auth(client):
    r = client.post('/jobs', json={'script_text': 'hello'})
    assert r.status_code == 401


def test_jobs_accepts_bearer(client):
    key = os.environ['MEDIA_ENGINE_API_KEY']
    with patch('app.api.jobs.jobs.create_job', new_callable=AsyncMock) as mock_create:
        from datetime import datetime, timezone
        from uuid import uuid4
        mock_create.return_value = {
            'id': uuid4(),
            'script_text': 'hello',
            'status': 'pending',
            'created_at': datetime.now(timezone.utc),
            'updated_at': datetime.now(timezone.utc),
        }
        r = client.post(
            '/jobs',
            json={'script_text': 'hello'},
            headers={'Authorization': f'Bearer {key}'},
        )
    assert r.status_code == 201


def test_jobs_accepts_x_api_key(client):
    key = os.environ['MEDIA_ENGINE_API_KEY']
    with patch('app.api.jobs.jobs.get_job', new_callable=AsyncMock) as mock_get:
        mock_get.return_value = None
        r = client.get(
            '/jobs/00000000-0000-0000-0000-000000000001',
            headers={'X-API-Key': key},
        )
    assert r.status_code == 404  # auth passed; job missing


def test_webhooks_bypass_api_key(client):
    r = client.post('/webhooks/heygen', content=b'{}', headers={'content-type': 'application/json'})
    # Through middleware; HMAC/secret failure — not 401 from API key middleware alone
    # (could be 401 InvalidHeyGenSignature or 503 secret not configured)
    assert r.status_code in (400, 401, 503)
