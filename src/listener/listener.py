"""
listener.py — PostgreSQL NOTIFY/LISTEN listener for the verification service.

Listens on the reports_table_changes channel. When an insert notification
arrives, calls the on_notification callback with the raw JSON payload
string (which the caller is responsible for parsing).

Use listen_async_mock() during development (no pipeline DB required).
Use listen_async() in production.

Note: correlating a notification's experiment_id to a run_id is NOT done
here — see main.py's expected_experiment_to_run, which is built by
watching manifests_dir directly (the explicit hand-off). This module's
only job is delivering a raw payload for each insert.
"""

import asyncio
import json

import asyncpg

CHANNEL = "reports_table_changes"
RETRY_DELAY_SECONDS = 5


async def listen_async_mock(config: dict, on_notification) -> None:
    """Simulate a single NOTIFY event then sleep indefinitely."""
    payload = {
        "schema": "public",
        "op": "insert",
        "experimentId": "T087_run3_compressed_1",
        "name": "T087_run3_compressed_1_260514_1504",
        "user": "mock_user",
    }
    await on_notification(json.dumps(payload))
    await asyncio.sleep(float("inf"))


async def listen_async(config: dict, token: asyncio.Event, on_notification) -> None:
    """Listen on reports_table_changes and call on_notification for INSERT events."""
    db_url = config["pipeline"]["database_connection"]

    while not token.is_set():
        try:
            conn = await asyncpg.connect(db_url)
            try:
                async def _handler(conn, pid, channel, payload):
                    try:
                        data = json.loads(payload)
                        if data.get("op") == "insert":
                            await on_notification(payload)
                    except Exception as e:
                        print(f"Notification handler error: {e}")

                conn_terminated = asyncio.Event()
                conn.add_termination_listener(lambda _: conn_terminated.set())

                await conn.add_listener(CHANNEL, _handler)
                print(f"Listening on channel '{CHANNEL}'")

                # Wait for shutdown OR connection drop — whichever comes first.
                # Without this, a dropped connection leaves the service silently idle.
                done, pending = await asyncio.wait(
                    [
                        asyncio.ensure_future(token.wait()),
                        asyncio.ensure_future(conn_terminated.wait()),
                    ],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()

                if not conn.is_closed():
                    await conn.remove_listener(CHANNEL, _handler)
            finally:
                await conn.close()

        except asyncio.CancelledError:
            raise
        except Exception as e:
            if token.is_set():
                break
            print(f"Listener error: {e}. Retrying in {RETRY_DELAY_SECONDS}s...")
            await asyncio.sleep(RETRY_DELAY_SECONDS)
