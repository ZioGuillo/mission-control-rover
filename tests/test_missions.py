def test_path_returns_501(client):
    r = client.post("/api/missions/path")
    assert r.status_code == 501
    assert r.json()["detail"]["ok"] is False
    assert "path" in r.json()["detail"]["message"].lower()


def test_search_returns_501(client):
    r = client.post("/api/missions/search")
    assert r.status_code == 501
    assert r.json()["detail"]["ok"] is False
    assert "search" in r.json()["detail"]["message"].lower()
