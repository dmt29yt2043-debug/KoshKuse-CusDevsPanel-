"""Агент на Маке: забирает задачи с сервера, гонит аудио через MacWhisper, разбирает в Claude.

ARCHITECTURE.md, Р2, Р9, Р10, «Контракт агент ↔ сервер».

    python -m cusdev.agent run        # бесконечный цикл (так его запускает launchd)
    python -m cusdev.agent once       # один шаг — для проверки руками
    python -m cusdev.agent install    # положить plist в ~/Library/LaunchAgents и запустить
    python -m cusdev.agent uninstall

Одна задача за раз. Что в работе — в PIPELINE_DIR/state.json, поэтому перезапуск
(или выключенный Мак) ничего не теряет: агент продолжит с того же шага.
"""

import argparse
import json
import logging
import os
import plistlib
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from cusdev.config import Settings, get_settings
from cusdev.parse import CliError, ParseError, ocr_image, parse_transcript
from cusdev.taxonomy import get_taxonomy

log = logging.getLogger("cusdev.agent")

LABEL = "com.koshkuse.cusdev-agent"
MAX_CLI_ATTEMPTS = 5  # столько раз подряд CLI может не отработать, прежде чем задача упадёт
STABLE_CHECK_SEC = 3


class ServerError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


@dataclass
class State:
    id: int
    kind: str
    filename: str
    channel: str
    sku_hint: str | None
    step: str = "download"  # download → transcribe → parse → done
    started_at: float = 0.0
    transcript: str | None = None
    cli_failures: int = 0


# ───────────────────────── HTTP к серверу ─────────────────────────


class Server:
    def __init__(self, settings: Settings) -> None:
        self.base = settings.agent_server_url.rstrip("/")
        self.token = settings.agent_token

    def call(self, method: str, path: str, body: bytes | None = None,
             content_type: str = "application/json") -> tuple[int, bytes]:  # fmt: skip
        req = urllib.request.Request(self.base + path, data=body, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if body is not None:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise ServerError(exc.code, detail) from exc

    def post_json(self, path: str, payload: dict) -> tuple[int, bytes]:
        return self.call("POST", path, json.dumps(payload, ensure_ascii=False).encode())


# ───────────────────────── state.json ─────────────────────────


def _state_path(settings: Settings) -> Path:
    return settings.pipeline_dir / "state.json"


def load_state(settings: Settings) -> State | None:
    path = _state_path(settings)
    if not path.exists():
        return None
    return State(**json.loads(path.read_text(encoding="utf-8")))


def save_state(settings: Settings, state: State | None) -> None:
    path = _state_path(settings)
    if state is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ───────────────────────── файлы ─────────────────────────


def inbox(settings: Settings) -> Path:
    return settings.pipeline_dir / "inbox"


def find_transcript(folder: Path, audio_name: str) -> Path | None:
    """.txt, который MacWhisper положил рядом с аудио. Имя сравниваем строкой, а не glob:
    в имени есть «[abc123]», а квадратные скобки — спецсимволы glob."""
    stem = Path(audio_name).stem
    candidates = [p for p in folder.iterdir() if p.suffix == ".txt" and p.stem.startswith(stem)]
    return min(candidates, key=lambda p: len(p.name)) if candidates else None


def is_stable(path: Path) -> bool:
    """MacWhisper мог ещё не дописать файл: размер не меняется несколько секунд."""
    size = path.stat().st_size
    time.sleep(STABLE_CHECK_SEC)
    return size > 0 and path.stat().st_size == size


def archive(settings: Settings, *files: Path) -> None:
    dest = settings.pipeline_dir / "_processed" / datetime.now().strftime("%Y-%m")
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        if f.exists():
            shutil.move(str(f), dest / f.name)


# ───────────────────────── шаг агента ─────────────────────────


def _fail(server: Server, settings: Settings, state: State, reason: str) -> None:
    log.warning("задача %s упала: %s", state.id, reason)
    try:
        server.post_json(f"/api/agent/jobs/{state.id}/fail", {"reason": reason})
    except ServerError as exc:
        log.warning("сервер не принял fail: %s", exc)
    save_state(settings, None)


def step(settings: Settings, server: Server) -> bool:
    """Один шаг. True — была работа (можно сразу следующий шаг), False — ждём."""
    state = load_state(settings)
    if state is None:
        status, body = server.call("POST", "/api/agent/next")
        if status == 204:
            return False
        job = json.loads(body)
        state = State(**job, started_at=time.time())
        save_state(settings, state)
        log.info("взял задачу %s (%s): %s", state.id, state.kind, state.filename)

    try:
        return _advance(settings, server, state)
    except ServerError as exc:
        if exc.status in (404, 409, 410):
            # сервер уже вернул задачу в очередь (истекла аренда) или её больше нет —
            # бросаем локальное состояние, следующий next выдаст актуальную
            log.warning("сервер отказал по задаче %s: %s — сбрасываю", state.id, exc)
            save_state(settings, None)
            return True
        raise


def _advance(settings: Settings, server: Server, state: State) -> bool:
    folder = inbox(settings)
    folder.mkdir(parents=True, exist_ok=True)
    work = settings.pipeline_dir / "work"
    work.mkdir(parents=True, exist_ok=True)
    source = (folder if state.kind == "audio" else work) / state.filename

    if state.step == "download":
        _, data = server.call("GET", f"/api/agent/jobs/{state.id}/source")
        if state.kind == "text":
            state.transcript = data.decode("utf-8")
            state.step = "parse"
        else:
            source.write_bytes(data)
            state.step = "transcribe"
        save_state(settings, state)
        return True

    if state.step == "transcribe":
        if state.kind == "image":
            try:
                state.transcript = ocr_image(source, settings.parse_model)
            except CliError as exc:
                return _cli_trouble(server, settings, state, exc)
            except ParseError as exc:
                _fail(server, settings, state, str(exc))
                archive(settings, source)
                return True
        else:
            txt = find_transcript(folder, state.filename)
            if txt is None or not is_stable(txt):
                waited = (time.time() - state.started_at) / 60
                if waited > settings.transcribe_timeout_minutes:
                    _fail(server, settings, state,
                          f"MacWhisper не выдал текст за {int(waited)} мин — проверьте, что папка "
                          f"{folder} добавлена в Watched Folders")  # fmt: skip
                    archive(settings, source)
                return False
            state.transcript = txt.read_text(encoding="utf-8", errors="replace")
            archive(settings, txt)
            server.call("POST", f"/api/agent/jobs/{state.id}/transcript",
                        state.transcript.encode(), "text/plain; charset=utf-8")  # fmt: skip
        state.step = "parse"
        save_state(settings, state)
        return True

    if state.step == "parse":
        try:
            parsed = parse_transcript(
                state.transcript or "",
                model=settings.parse_model,
                taxonomy=get_taxonomy(),
                channel=state.channel,
                sku_hint=state.sku_hint,
            )
        except CliError as exc:
            return _cli_trouble(server, settings, state, exc)
        except ParseError as exc:
            _fail(server, settings, state, f"разбор не удался: {exc}")
            archive(settings, source)
            return True
        server.post_json(
            f"/api/agent/jobs/{state.id}/result",
            {
                "transcript": state.transcript,
                "result": parsed.result.model_dump(),
                "warnings": parsed.warnings,
                "model": parsed.model,
            },
        )
        archive(settings, source)
        save_state(settings, None)
        log.info("задача %s готова: отзывов %s", state.id, len(parsed.result.items))
        return True

    raise RuntimeError(f"неизвестный шаг {state.step}")


def _cli_trouble(server: Server, settings: Settings, state: State, exc: Exception) -> bool:
    """CLI не отработал — скорее всего истёк токен или лимит. Задачу не роняем сразу."""
    state.cli_failures += 1
    save_state(settings, state)
    log.error("claude CLI не отработал (%s/%s): %s", state.cli_failures, MAX_CLI_ATTEMPTS, exc)
    if state.cli_failures >= MAX_CLI_ATTEMPTS:
        _fail(server, settings, state, f"Claude на Маке не отвечает: {exc}")
    return False


def run(settings: Settings, once: bool = False) -> None:
    server = Server(settings)
    log.info("агент запущен: %s, папка %s", server.base, settings.pipeline_dir)
    while True:
        busy = False
        try:
            server.call("POST", "/api/agent/heartbeat")
            busy = step(settings, server)
        except (ServerError, urllib.error.URLError, OSError) as exc:
            log.warning("нет связи с сервером: %s", exc)
        if once:
            return
        if not busy:
            time.sleep(settings.agent_poll_seconds)


# ───────────────────────── launchd ─────────────────────────


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def install(settings: Settings) -> None:
    repo = Path(__file__).resolve().parent.parent
    python = repo / ".venv" / "bin" / "python"
    claude = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
    settings.pipeline_dir.mkdir(parents=True, exist_ok=True)
    inbox(settings).mkdir(exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [str(python), "-m", "cusdev.agent", "run"],
        "WorkingDirectory": str(repo),  # отсюда читается .env
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 60,
        "EnvironmentVariables": {
            "PATH": f"{Path(claude).parent}:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin",
            "PYTHONUNBUFFERED": "1",
        },
        "StandardOutPath": str(settings.pipeline_dir / "agent.log"),
        "StandardErrorPath": str(settings.pipeline_dir / "agent.log"),
    }
    path = _plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
    path.write_bytes(plistlib.dumps(plist))
    subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True)
    print(f"Агент установлен: {path}\nЛог: {settings.pipeline_dir / 'agent.log'}")
    print(f"Добавьте папку {inbox(settings)} в MacWhisper → Settings → Watched Folders.")


def uninstall() -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    _plist_path().unlink(missing_ok=True)
    print("Агент остановлен и удалён из автозапуска.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("command", choices=["run", "once", "install", "uninstall"])
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    settings.pipeline_dir = settings.pipeline_dir.expanduser()
    if args.command in ("run", "once") and not settings.agent_token:
        print("Нет AGENT_TOKEN в .env", file=sys.stderr)
        return 2
    if args.command == "install":
        install(settings)
    elif args.command == "uninstall":
        uninstall()
    else:
        run(settings, once=args.command == "once")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
