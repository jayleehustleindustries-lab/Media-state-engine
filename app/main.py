from contextlib import asynccontextmanager
from fastapi import FastAPI
from .db import connect, close
from .api.jobs import router as jobs_router
from .auth import ApiKeyMiddleware


@asynccontextmanager
async def lifespan(app):
    await connect()
    yield
    await close()


app = FastAPI(title='Media State Engine', version='1.1.0', lifespan=lifespan)
app.add_middleware(ApiKeyMiddleware)
app.include_router(jobs_router)


@app.get('/health')
async def health():
    return {'status': 'ok'}
