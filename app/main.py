from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse
from .db import connect, close
from .api.jobs import router as jobs_router
from .api.avatar import router as avatar_router
from .auth import ApiKeyMiddleware


@asynccontextmanager
async def lifespan(app):
    await connect()
    yield
    await close()


app = FastAPI(title='Media State Engine', version='1.5.0', lifespan=lifespan)
app.add_middleware(ApiKeyMiddleware)
app.include_router(jobs_router)
app.include_router(avatar_router)


@app.get('/avatar', include_in_schema=False)
async def avatar_intake_page():
    """Serve the password-style, API-key-protected Avatar Intake interface."""
    return FileResponse(Path(__file__).parent / 'templates' / 'avatar_intake.html')


@app.get('/health')
async def health():
    return {'status': 'ok'}
