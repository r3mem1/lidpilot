"""PostToolUse-hook Claude Code: после Edit/Write/MultiEdit проверяет изменённый .py файл через ruff.

Экономит ходы: ошибка стиля/багбира видна сразу, а не при позднем полном прогоне линтеров.
При чистом файле молчит (ноль токенов); при замечаниях печатает их в stderr и
завершается кодом 2 — Claude Code показывает вывод модели. Файл НЕ переформатируется
(только проверка), чтобы не ломать «файл изменился с момента чтения».
Вход — JSON события hook со stdin (`tool_input.file_path`).
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 - фиксированный вызов ruff без shell
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAX_LINES = 30


def main() -> int:
    try:
        event = json.load(sys.stdin)
        file_path = Path(event["tool_input"]["file_path"]).resolve()
    except (json.JSONDecodeError, KeyError, TypeError, OSError):
        return 0  # не наше событие — не мешаем работе

    ruff = shutil.which("ruff")
    if ruff is None or file_path.suffix != ".py" or not file_path.is_file():
        return 0
    if ROOT not in file_path.parents or "migrations" in file_path.relative_to(ROOT).parts[:2]:
        return 0  # вне проекта; миграции исключены из линтинга проекта

    proc = subprocess.run(  # nosec B603  # noqa: S603 - фиксированная команда, без shell
        [ruff, "check", "--quiet", "--output-format", "concise", str(file_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode == 0:
        return 0
    lines = (proc.stdout + proc.stderr).strip().splitlines()
    print(f"ruff: замечания в {file_path.relative_to(ROOT).as_posix()}", file=sys.stderr)
    print("\n".join(lines[:MAX_LINES]), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
