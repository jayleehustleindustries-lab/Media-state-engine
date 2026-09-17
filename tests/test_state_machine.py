import asyncio
from uuid import uuid4
import pytest
from app.state_machine import advance, IllegalTransition

class FakeConn:
    def __init__(self, status='pending'):
        self.job = {'id': uuid4(), 'status': status}
        self.events = []
        self.lock = asyncio.Lock()
    async def fetchrow(self, sql, *args):
        if 'FOR UPDATE' in sql:
            await self.lock.acquire()
            return dict(self.job)
        if sql.startswith('UPDATE'):
            self.job['status'] = args[1]
            self.lock.release()
            return dict(self.job)
        return None
    async def execute(self, sql, *args):
        if sql.startswith('INSERT INTO events'):
            self.events.append({'from': args[1], 'to': args[2]})

@pytest.mark.asyncio
async def test_advance_logs_event_and_changes_status():
    conn = FakeConn()
    result = await advance(conn, conn.job['id'], 'script_ready', {})
    assert result['status'] == 'script_ready'
    assert conn.events == [{'from': 'pending', 'to': 'script_ready'}]

@pytest.mark.asyncio
async def test_illegal_transition_has_zero_events():
    conn = FakeConn()
    with pytest.raises(IllegalTransition):
        await advance(conn, conn.job['id'], 'rendered', {})
    assert conn.events == []

@pytest.mark.asyncio
async def test_concurrent_advance_has_one_winner():
    conn = FakeConn()
    async def attempt():
        try:
            await advance(conn, conn.job['id'], 'script_ready', {})
            return True
        except IllegalTransition:
            return False
    results = await asyncio.gather(attempt(), attempt())
    assert sorted(results) == [False, True]
    assert len(conn.events) == 1
