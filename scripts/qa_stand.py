"""
Стенд ручной проверки сайта «глазами пользователя» (НЕ часть приложения).

Поднимает изолированную копию LeadPilot: своя SQLite-база (qa_site.db), свои
секреты, AI в режиме stub, Sentry выключен и поддельный Telegram Bot API, который
ничего никуда не отправляет, а записывает всё, что «ушло» клиентам и мастерам,
в .qa/telegram.json. Рабочая база, .env-секреты и реальные боты не затрагиваются.

    python scripts/qa_stand.py up              # сбросить стенд, демо-данные, сайт :8770
    python scripts/qa_stand.py up --keep       # перезапуск без сброса данных
    python scripts/qa_stand.py say 1001 "какие окна на завтра?" --name Олег
    python scripts/qa_stand.py inbox [1001]     # что получил клиент (или все чаты)

Вход: владелец demo@example.com, менеджер manager@example.com, админ платформы
admin@example.com — пароль у всех Demo-Pass-123. Токен бота в кабинете — любой
вида 123456:ABC… (поддельный Telegram примет его); webhook-секрет стенд
перехватывает сам, поэтому `say` работает сразу после подключения бота.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # nosec B404 - запуск alembic и seed_demo с фиксированными аргументами
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QA = ROOT / ".qa"
DB = ROOT / "qa_site.db"
SITE_PORT = 8770
TG_PORT = 8771
PASSWORD = "Demo-Pass-123"  # noqa: S105  # nosec B105 - демо-аккаунты стенда

ENV = {
    "ENVIRONMENT": "development",
    "DATABASE_URL": f"sqlite:///{DB.as_posix()}",
    "JWT_SECRET": "qa-stand-jwt-secret-not-for-production-0123456789",  # nosec B105 - только стенд
    "SECRETS_ENCRYPTION_KEY": "qa-stand-encryption-key-not-for-production",
    "AUTH_COOKIE_SECURE": "false",
    "AI_PROVIDER": "stub",
    "AI_API_KEY": "",
    "SENTRY_DSN": "",
    "TELEGRAM_API_BASE_URL": f"http://127.0.0.1:{TG_PORT}",
    "VK_API_BASE_URL": f"http://127.0.0.1:{TG_PORT}/vk",
    # Telegram требует https-адрес webhook; его увидит только поддельный Telegram,
    # а `say` шлёт обновления прямо на локальный сайт.
    "PUBLIC_BASE_URL": "https://qa-stand.invalid",
    "BOOTSTRAP_ADMIN_EMAIL": "admin@example.com",
    "BOOTSTRAP_ADMIN_PASSWORD": PASSWORD,
    "REPLY_DEBOUNCE_SECONDS": "1",
    "TELEGRAM_MAX_RETRIES": "0",
    "PYTHONUTF8": "1",
}


# --------------------------------------------------------------------------- #
# Поддельный Telegram Bot API
# --------------------------------------------------------------------------- #
_lock = threading.Lock()


def _load() -> dict:
    path = QA / "telegram.json"
    return json.loads(path.read_text("utf-8")) if path.exists() else {"webhooks": {}, "sent": []}


def _save(state: dict) -> None:
    (QA / "telegram.json").write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")


class FakeTelegram(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # тишина в консоли
        return

    def do_POST(self) -> None:  # noqa: N802 - имя из http.server
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            payload = {}
        parts = self.path.strip("/").split("/")
        token = parts[0].removeprefix("bot") if parts else ""
        method = parts[-1] if parts else ""
        result: object = True
        with _lock:
            state = _load()
            if method == "getMe":
                result = {
                    "id": 7000001,
                    "is_bot": True,
                    "first_name": "QA бот",
                    "username": "qa_bot",
                }
            elif method == "setWebhook":
                state["webhooks"][token[:12]] = payload.get("secret_token")
            elif method == "sendMessage":
                state["sent"].append(
                    {
                        "chat_id": str(payload.get("chat_id")),
                        "text": payload.get("text"),
                        "buttons": [
                            btn.get("text")
                            for row in (payload.get("reply_markup") or {}).get("keyboard", [])
                            for btn in row
                        ],
                        "at": time.strftime("%H:%M:%S"),
                    }
                )
                result = {"message_id": len(state["sent"]), "date": int(time.time())}
            _save(state)
        body = json.dumps({"ok": True, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


# --------------------------------------------------------------------------- #
# Команды
# --------------------------------------------------------------------------- #
def up(keep: bool) -> None:
    QA.mkdir(exist_ok=True)
    if not keep:
        DB.unlink(missing_ok=True)
        _save({"webhooks": {}, "sent": []})
    env = {**os.environ, **ENV}
    subprocess.run(  # nosec B603 - фиксированная команда
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env, check=True
    )
    subprocess.run(  # nosec B603 - фиксированная команда
        [sys.executable, "scripts/seed_demo.py"], cwd=ROOT, env=env, check=True
    )
    server = ThreadingHTTPServer(("127.0.0.1", TG_PORT), FakeTelegram)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Поддельный Telegram: http://127.0.0.1:{TG_PORT}")
    print(f"Сайт: http://127.0.0.1:{SITE_PORT}  (Ctrl+C — остановить)")
    os.environ.update(ENV)
    import uvicorn

    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    uvicorn.run("main:app", host="127.0.0.1", port=SITE_PORT, log_level="warning")


def say(chat_id: str, text: str, name: str, wait: float) -> None:
    import httpx

    secrets = [s for s in _load()["webhooks"].values() if s]
    if not secrets:
        sys.exit("Бот не подключён: подключите Telegram в кабинете (Настройки → Каналы)")
    before = len(_load()["sent"])
    update_id = int(time.time() * 1000) % 2_000_000_000
    r = httpx.post(
        f"http://127.0.0.1:{SITE_PORT}/webhooks/telegram",
        headers={"X-Telegram-Bot-Api-Secret-Token": secrets[-1]},
        json={
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "date": int(time.time()),
                "chat": {"id": int(chat_id), "type": "private", "first_name": name},
                "from": {"id": int(chat_id), "is_bot": False, "first_name": name},
                "text": text,
            },
        },
        timeout=30,
    )
    print(f"webhook → HTTP {r.status_code}")
    deadline = time.time() + wait
    while time.time() < deadline:
        new = [m for m in _load()["sent"][before:] if m["chat_id"] == str(chat_id)]
        if new:
            time.sleep(1.5)  # вдруг ответ из нескольких частей
            break
        time.sleep(0.5)
    inbox(chat_id, since=before)


def inbox(chat_id: str | None, since: int = 0) -> None:
    sent = _load()["sent"][since:]
    rows = [m for m in sent if chat_id is None or m["chat_id"] == str(chat_id)]
    if not rows:
        print("(клиенту ничего не отправлено)")
    for m in rows:
        buttons = "".join(f" [ {b} ]" for b in m.get("buttons") or [])
        print(f"[{m['at']}] → {m['chat_id']}:\n{m['text']}")
        print(f"кнопки:{buttons}\n" if buttons else "")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_up = sub.add_parser("up")
    p_up.add_argument("--keep", action="store_true", help="не сбрасывать базу и переписку")
    p_say = sub.add_parser("say")
    p_say.add_argument("chat_id")
    p_say.add_argument("text")
    p_say.add_argument("--name", default="Клиент")
    p_say.add_argument("--wait", type=float, default=12)
    p_inbox = sub.add_parser("inbox")
    p_inbox.add_argument("chat_id", nargs="?")
    args = parser.parse_args()
    if args.cmd == "up":
        up(args.keep)
    elif args.cmd == "say":
        say(args.chat_id, args.text, args.name, args.wait)
    else:
        inbox(args.chat_id)
