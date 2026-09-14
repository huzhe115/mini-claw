"""网关基础接口:健康检查、认证、会话增查。"""


async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_api_requires_token(client):
    resp = await client.get("/api/sessions")
    assert resp.status_code == 401

    resp = await client.get("/api/sessions", headers={"X-Gateway-Token": "wrong"})
    assert resp.status_code == 401


async def test_create_and_list_sessions(client, auth_headers):
    resp = await client.post("/api/sessions", headers=auth_headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] and body["title"] == "新会话" and body["created_at"]

    resp = await client.get("/api/sessions", headers=auth_headers)
    assert resp.status_code == 200
    assert [s["id"] for s in resp.json()] == [body["id"]]


async def test_messages_404(client, auth_headers):
    resp = await client.get("/api/sessions/nope/messages", headers=auth_headers)
    assert resp.status_code == 404


async def test_index_page(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "Mini-Claw" in resp.text
