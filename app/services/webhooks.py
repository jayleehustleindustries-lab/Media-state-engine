import hashlib, hmac, httpx, json
from uuid import UUID
from ..config import settings

async def delivery(job_id: UUID, payload: dict):
    if not settings.webhook_url: return None
    body = json.dumps({'job_id': str(job_id), **payload}, separators=(',', ':')).encode()
    signature = hmac.new(settings.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(settings.webhook_url, content=body, headers={'content-type': 'application/json', 'x-webhook-signature': signature})
        response.raise_for_status()
    return response
