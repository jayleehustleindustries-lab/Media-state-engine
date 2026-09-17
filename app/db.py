from contextlib import asynccontextmanager
import asyncpg
from .config import settings

_pool: asyncpg.Pool | None = None

async def connect() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=10)
    return _pool

async def close() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None

@asynccontextmanager
async def transaction():
    pool = await connect()
    async with pool.acquire() as conn:
        async with conn.transaction():
            yield conn
