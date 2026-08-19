from fastapi.testclient import TestClient

from agents.main import app

client = TestClient(app)


def test_health():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_chat_placeholder():
    res = client.post(
        "/chat",
        json={
            "scenario_id": "test",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert res.status_code == 200
    assert "reply" in res.json()
