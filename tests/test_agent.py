"""Сквозной тест агента: настоящий HTTP-сервер в потоке, MacWhisper и claude подменены."""

import json
import socket
import stat
import threading
import time

import pytest
import uvicorn
from conftest import AGENT

from cusdev import agent
from cusdev.models import Conversation
from cusdev.parse import CliError

TRANSCRIPT = "Индейку она съела сразу, вылизала миску. А утку понюхала и ушла."
RESULT = {
    "summary": "Индейка зашла.",
    "speakers": "one",
    "items": [
        {"sku": "turkey", "verdict": "liked", "themes": [], "quotes": [{"text": "вылизала миску"}]}
    ],
}


@pytest.fixture
def live(client, settings, taxonomy, tmp_path, monkeypatch):
    """Поднимает приложение (с подменами из фикстуры client) на свободном порту."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(client.app, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)

    settings.agent_server_url = f"http://127.0.0.1:{port}"
    settings.pipeline_dir = tmp_path / "pipeline"
    monkeypatch.setattr(agent, "STABLE_CHECK_SEC", 0)
    monkeypatch.setattr(agent, "get_taxonomy", lambda: taxonomy)
    yield settings
    server.should_exit = True
    thread.join(timeout=5)


def fake_claude(tmp_path, monkeypatch, payload: dict) -> None:
    script = tmp_path / "claude"
    out = json.dumps({"is_error": False, "structured_output": payload}, ensure_ascii=False)
    script.write_text(f"#!/bin/sh\ncat > /dev/null\ncat <<'EOF'\n{out}\nEOF\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("CLAUDE_BIN", str(script))


def drain(settings, server, limit=10) -> None:
    for _ in range(limit):
        if not agent.step(settings, server):
            return


def test_audio_goes_through_macwhisper_and_claude(live, client, session, tmp_path, monkeypatch):
    fake_claude(tmp_path, monkeypatch, RESULT)
    conv_id = client.post(
        "/api/upload", data={"tester": "Анна К."}, files=[("files", ("a.m4a", b"audio"))]
    ).json()[0]["id"]
    server = agent.Server(live)

    drain(live, server)  # скачал и ждёт MacWhisper
    state = agent.load_state(live)
    assert state.step == "transcribe"
    audio = agent.inbox(live) / state.filename
    assert audio.read_bytes() == b"audio"

    # «MacWhisper» дописал текст рядом с аудио
    audio.with_suffix(".txt").write_text(TRANSCRIPT, encoding="utf-8")
    drain(live, server)

    assert agent.load_state(live) is None
    conv = session.get(Conversation, conv_id)
    session.refresh(conv)
    assert conv.status == "done" and conv.transcript == TRANSCRIPT
    assert [q.text for q in conv.items[0].quotes] == ["вылизала миску"]
    archived = list((live.pipeline_dir / "_processed").rglob("*"))
    assert {p.suffix for p in archived if p.is_file()} == {".m4a", ".txt"}
    assert not any(agent.inbox(live).iterdir())


def test_text_job_skips_macwhisper(live, client, session, tmp_path, monkeypatch):
    fake_claude(tmp_path, monkeypatch, RESULT)
    conv_id = client.post("/api/text", json={"tester": "Ольга М.", "text": TRANSCRIPT}).json()["id"]
    drain(live, agent.Server(live))
    conv = session.get(Conversation, conv_id)
    session.refresh(conv)
    assert conv.status == "done"


def test_cli_trouble_keeps_job_then_fails_after_limit(live, client, session, monkeypatch):
    def broken(*_args, **_kw):
        raise CliError("claude CLI: OAuth token has expired")

    monkeypatch.setattr(agent, "parse_transcript", broken)
    conv_id = client.post("/api/text", json={"tester": "Ольга М.", "text": TRANSCRIPT}).json()["id"]
    server = agent.Server(live)
    drain(live, server)
    assert agent.load_state(live).cli_failures == 1  # задача не упала, ждёт починки токена
    for _ in range(agent.MAX_CLI_ATTEMPTS):
        agent.step(live, server)
    conv = session.get(Conversation, conv_id)
    session.refresh(conv)
    assert conv.status == "failed" and "expired" in conv.error


def test_stale_local_state_is_dropped_when_server_refuses(live, client, tmp_path, monkeypatch):
    fake_claude(tmp_path, monkeypatch, RESULT)
    conv_id = client.post("/api/text", json={"tester": "Ольга М.", "text": TRANSCRIPT}).json()["id"]
    server = agent.Server(live)
    agent.step(live, server)  # взял задачу и скачал текст
    # пока агент «спал», задачу закрыли на сервере (например, истекла аренда)
    client.post(f"/api/agent/jobs/{conv_id}/fail", headers=AGENT, json={"reason": "вручную"})
    agent.step(live, server)  # разбор → сервер отвечает 409 → локальное состояние сброшено
    assert agent.load_state(live) is None


def test_find_transcript_handles_brackets(tmp_path):
    (tmp_path / "2026-10-07 12.30 Анна К. [abc123].txt").write_text("x")
    (tmp_path / "другое.txt").write_text("y")
    found = agent.find_transcript(tmp_path, "2026-10-07 12.30 Анна К. [abc123].m4a")
    assert found.name == "2026-10-07 12.30 Анна К. [abc123].txt"
    assert agent.find_transcript(tmp_path, "нет такого [zzz].m4a") is None
