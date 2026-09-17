import httpx
from ..config import settings

async def generate(script_text: str, job_id: str) -> dict:
    if not settings.elevenlabs_api_key or not settings.elevenlabs_voice_id:
        raise RuntimeError('ElevenLabs is not configured')
    url = f'{settings.elevenlabs_url}/{settings.elevenlabs_voice_id}'
    headers = {'xi-api-key': settings.elevenlabs_api_key, 'accept': 'audio/mpeg', 'content-type': 'application/json'}
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(url, headers=headers, json={'text': script_text, 'model_id': 'eleven_multilingual_v2'})
        response.raise_for_status()
        return {'url': response.headers.get('location'), 'storage_path': f'audio/{job_id}.mp3', 'meta': {'content_type': 'audio/mpeg', 'bytes': len(response.content)}}
