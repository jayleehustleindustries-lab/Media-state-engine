from contextlib import asynccontextmanager
from fastapi import FastAPI
from .db import connect, close
from .api.jobs import router as jobs_router

@asynccontextmanager
async def lifespan(app):
    await connect()
    yield
    await close()

app = FastAPI(title='Media State Engine', version='1.0.0', lifespan=lifespan)
app.include_router(jobs_router)

@app.get('/health')
async def health(): return {'status': 'ok'}
