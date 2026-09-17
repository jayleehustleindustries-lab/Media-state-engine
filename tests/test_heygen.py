import hashlib
import hmac
import json
import pytest
from app.services.heygen import InvalidHeyGenSignature, parse_webhook, webhook_video


def signed(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_valid_signature_and_webhook_extraction():
    body = json.dumps(
        {
            'event_type': 'avatar_video.success',
            'event_data': {'video_id': 'vid_123', 'video_url': 'https://cdn/video.mp4'},
        },
        separators=(',', ':'),
    ).encode()
    payload = parse_webhook(body, signed(body, 'secret'), 'secret')
    event_type, video_id, data = webhook_video(payload)
    assert event_type == 'avatar_video.success'
    assert video_id == 'vid_123'
    assert data['video_url'].endswith('.mp4')


def test_invalid_signature_is_rejected():
    body = b'{"event_type":"avatar_video.success"}'
    with pytest.raises(InvalidHeyGenSignature):
        parse_webhook(body, 'bad', 'secret')
