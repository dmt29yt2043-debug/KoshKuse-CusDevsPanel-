from datetime import timedelta

from conftest import AGENT

from cusdev import queue
from cusdev.models import Conversation

TRANSCRIPT = "Индейку она съела сразу, вылизала миску. А утку понюхала и ушла."
RESULT = {
    "summary": "Индейка зашла, утка нет.",
    "speakers": "one",
    "items": [
        {
            "sku": "turkey",
            "verdict": "liked",
            "themes": ["palatability"],
            "quotes": [{"text": "вылизала миску"}, {"text": "кошка в восторге"}],
        },
        {"sku": "duck", "verdict": "disliked", "themes": ["smell"], "quotes": []},
    ],
}


def upload(client, name="call.m4a", data=b"fake-audio-1", **fields):
    form = {"tester": "Анна К.", **fields}
    resp = client.post("/api/upload", data=form, files=[("files", (name, data))])
    assert resp.status_code == 200, resp.text
    return resp.json()[0]


def test_upload_audio_then_duplicate_then_bad_format(client):
    first = upload(client, sku_hint="turkey", talked_at="2026-10-07T12:30:00+03:00")
    assert first["result"] == "added"
    again = upload(client, name="call-copy.m4a")
    assert again == {**again, "result": "duplicate", "id": first["id"]}
    bad = upload(client, name="notes.pdf", data=b"x")
    assert bad["result"] == "error"
    assert client.get("/api/testers").json() == ["Анна К."]


def test_upload_rejects_unknown_sku(client):
    assert upload(client, sku_hint="rabbit")["result"] == "error"


def test_full_audio_flow_through_agent(client, session):
    conv_id = upload(client, talked_at="2026-10-07T12:30:00+03:00")["id"]

    job = client.post("/api/agent/next", headers=AGENT).json()
    assert job["id"] == conv_id and job["kind"] == "audio"
    assert job["filename"] == f"2026-10-07 12.30 Анна К. [{job['filename'][-11:-5]}].m4a"
    assert client.get(f"/api/agent/jobs/{conv_id}/source", headers=AGENT).content == b"fake-audio-1"
    assert client.post("/api/agent/next", headers=AGENT).status_code == 204  # одна за раз

    r = client.post(f"/api/agent/jobs/{conv_id}/transcript", headers=AGENT, content=TRANSCRIPT)
    assert r.status_code == 204
    r = client.post(
        f"/api/agent/jobs/{conv_id}/result",
        headers=AGENT,
        json={"transcript": TRANSCRIPT, "result": RESULT, "warnings": [], "model": "m"},
    )
    assert r.status_code == 204

    conv = session.get(Conversation, conv_id)
    assert conv.status == "done"
    assert [i.sku for i in conv.items] == ["turkey", "duck"]
    # выдуманная цитата не прошла серверный sanitize()
    assert [q.text for q in conv.items[0].quotes] == ["вылизала миску"]
    assert any("не найдена" in w for w in conv.parse_warnings)


def test_text_skips_transcription(client):
    r = client.post("/api/text", json={"tester": "Ольга М.", "text": TRANSCRIPT})
    conv_id = r.json()["id"]
    job = client.post("/api/agent/next", headers=AGENT).json()
    assert job["kind"] == "text" and job["channel"] == "chat"
    assert client.get(f"/api/agent/jobs/{conv_id}/source", headers=AGENT).text == TRANSCRIPT
    again = client.post("/api/text", json={"tester": "Ольга М.", "text": f"  {TRANSCRIPT}\n"})
    assert again.json()["result"] == "duplicate"


def test_stale_lease_returns_to_queue(client, session):
    conv_id = upload(client)["id"]
    client.post("/api/agent/next", headers=AGENT)
    conv = session.get(Conversation, conv_id)
    conv.claimed_at = queue.now() - timedelta(hours=3)
    session.commit()
    assert client.post("/api/agent/next", headers=AGENT).json()["id"] == conv_id


def test_fail_and_retry(client):
    conv_id = upload(client)["id"]
    client.post("/api/agent/next", headers=AGENT)
    r = client.post(f"/api/agent/jobs/{conv_id}/fail", headers=AGENT, json={"reason": "битый файл"})
    assert r.status_code == 204
    item = client.get("/api/queue").json()["items"][0]
    assert item["status"] == "failed" and item["error"] == "битый файл"
    assert client.post(f"/api/conversations/{conv_id}/retry").status_code == 204
    assert client.post("/api/agent/next", headers=AGENT).json()["id"] == conv_id


def test_result_for_job_not_in_work_is_rejected(client):
    conv_id = upload(client)["id"]
    r = client.post(
        f"/api/agent/jobs/{conv_id}/result",
        headers=AGENT,
        json={"transcript": TRANSCRIPT, "result": RESULT, "model": "m"},
    )
    assert r.status_code == 409


def test_agent_auth_and_online_status(client):
    assert client.post("/api/agent/heartbeat").status_code == 401
    assert client.get("/api/queue").json()["agent_online"] is False
    assert client.post("/api/agent/heartbeat", headers=AGENT).status_code == 204
    status = client.get("/api/queue").json()
    assert status["agent_online"] is True and status["queued"] == 0


def test_secret_link_sets_cookie(client, settings):
    settings.access_key = "team-key"
    assert client.get("/api/queue").status_code == 401
    assert client.get("/?key=wrong", follow_redirects=False).status_code == 401
    r = client.get("/?key=team-key", follow_redirects=False)
    assert r.status_code == 303 and "cusdev_key" in r.cookies
    assert client.get("/api/queue").status_code == 200
