import httpx
from ..config import settings

async def render(job: dict, audio: dict | None = None) -> dict:
    if not settings.remotion_render_url:
        raise RuntimeError('Remotion is not configured')
    async with httpx.AsyncClient(timeout=600) as client:
        response = await client.post(settings.remotion_render_url, json={'job_id': str(job['id']), 'script_text': job['script_text'], 'audio': audio or {}})
        response.raise_for_status()
        data = response.json()
        return {'url': data.get('url'), 'storage_path': data.get('storage_path', f'final/{job["id"]}.mp4'), 'meta': data}
