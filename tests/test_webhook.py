from fastapi.testclient import TestClient

from app import main


def test_webhook_rejects_bad_secret():
    client = TestClient(main.app)  # no `with`: lifespan (Telegram network calls) not run
    r = client.post("/webhook", json={"update_id": 1}, headers={"X-Telegram-Bot-Api-Secret-Token": "nope"})
    assert r.status_code == 403


def test_webhook_queues_update_and_returns_fast():
    client = TestClient(main.app)
    r = client.post(
        "/webhook",
        json={"update_id": 5},
        headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
    )
    assert r.status_code == 200
    assert main.application.update_queue.get_nowait().update_id == 5
