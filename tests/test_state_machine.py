"""Unit tests for transition validation (FakeConn is OK here — not concurrency claims)."""
from uuid import uuid4
import pytest
from app.state_machine import advance, IllegalTransition


class FakeConn:
    def __init__(self, status='pending'):
        self.job = {'id': uuid4(), 'status': status}
        self.events = []

    async def fetchrow(self, sql, *args):
        if 'FOR UPDATE' in sql:
            return dict(self.job)
        if sql.startswith('UPDATE'):
            self.job['status'] = args[1]
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
async def test_rendered_to_staged_allowed():
    conn = FakeConn(status='rendered')
    result = await advance(conn, conn.job['id'], 'staged', {})
    assert result['status'] == 'staged'


@pytest.mark.asyncio
async def test_staged_cannot_skip_to_delivered():
    conn = FakeConn(status='staged')
    with pytest.raises(IllegalTransition):
        await advance(conn, conn.job['id'], 'delivered', {})
    assert conn.events == []


@pytest.mark.asyncio
async def test_approve_then_deliver():
    conn = FakeConn(status='staged')
    await advance(conn, conn.job['id'], 'approved', {})
    result = await advance(conn, conn.job['id'], 'delivered', {})
    assert result['status'] == 'delivered'
