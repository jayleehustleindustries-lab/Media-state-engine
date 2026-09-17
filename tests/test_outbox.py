import json
import pytest
from app.services import outbox


class FakeOutboxConn:
    def __init__(self):
        self.rows = {}
        self._id = 0

    async def fetchrow(self, sql, *args):
        if 'INSERT INTO webhook_outbox' in sql:
            self._id += 1
            row = {
                'id': self._id,
                'job_id': args[0],
                'destination_url': args[1],
                'payload': json.loads(args[2]) if isinstance(args[2], str) else args[2],
                'max_attempts': args[3],
                'status': 'pending',
                'attempts': 0,
                'next_attempt_at': None,
                'last_error': None,
            }
            self.rows[self._id] = row
            return row
        return None

    async def fetch(self, sql, *args):
        if 'WITH due' in sql or 'status = \'delivering\'' in sql or 'FOR UPDATE SKIP LOCKED' in sql:
            claimed = []
            for row in list(self.rows.values()):
                if row['status'] == 'pending':
                    row['status'] = 'delivering'
                    claimed.append(dict(row))
            return claimed
        return []

    async def execute(self, sql, *args):
        oid = args[0]
        row = self.rows[oid]
        if 'status = \'delivered\'' in sql:
            row['status'] = 'delivered'
        elif "status = 'dead'" in sql:
            row['status'] = 'dead'
            row['attempts'] = args[1]
            row['last_error'] = args[2]
        elif "status = 'pending'" in sql:
            row['status'] = 'pending'
            row['attempts'] = args[1]
            row['last_error'] = args[2]


@pytest.mark.asyncio
async def test_outbox_enqueue_and_deliver():
    conn = FakeOutboxConn()
    from uuid import uuid4
    await outbox.enqueue(conn, uuid4(), {'status': 'rendered'}, destination_url='http://example.test/hook')
    assert len(conn.rows) == 1

    async def ok_deliver(url, payload):
        assert url == 'http://example.test/hook'
        assert payload['status'] == 'rendered'

    stats = await outbox.process_due(conn, deliver=ok_deliver)
    assert stats['delivered'] == 1
    assert list(conn.rows.values())[0]['status'] == 'delivered'


@pytest.mark.asyncio
async def test_outbox_retries_then_dead_letters():
    conn = FakeOutboxConn()
    from uuid import uuid4
    await outbox.enqueue(
        conn, uuid4(), {'status': 'rendered'},
        destination_url='http://example.test/hook', max_attempts=3,
    )

    async def fail_deliver(url, payload):
        raise RuntimeError('boom')

    for _ in range(3):
        # reset to pending if needed after claim
        for r in conn.rows.values():
            if r['status'] == 'delivering':
                r['status'] = 'pending'
        await outbox.process_due(conn, deliver=fail_deliver)

    # After max attempts the row should be dead
    row = list(conn.rows.values())[0]
    # process_due increments attempts each time; after 3 failures → dead
    assert row['status'] == 'dead'
    assert row['attempts'] >= 3
