"""Тихий запуск smoke-тестов и линтеров: экономия токенов при разработке (без потери диагностики).

Тесты: на каждый скрипт печатается только строка «ИТОГО» и упавшие проверки (`FAIL ...`);
если скрипт упал без итога (traceback) — последние строки вывода.
Линтеры (`lint`): ruff / ruff-format / pyright / bandit / pip-audit — строка итога на инструмент,
при ошибках — сами находки (bandit: одна строка на находку, ничего не отфильтровывается).
Полный вывод каждого прогона сохраняется в `.test_logs/<имя>.log` — открывать, только если нужен контекст.

    python scripts/run_checks.py                  # все smoke_test*.py
    python scripts/run_checks.py stage4 stage5    # по подстроке имени
    python scripts/run_checks.py lint             # все линтеры
    python scripts/run_checks.py lint ruff bandit # выбранные (по подстроке имени)
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 - запуск собственных тестов и линтеров проекта
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / ".test_logs"
TAIL_LINES = 25  # хвост вывода упавшего без итога скрипта
MAX_FINDINGS = 40  # максимум строк находок на инструмент (остальное — в логе)
LINE_WIDTH = 200

# Те же команды, что в CLAUDE.md («Команды»), из PATH; bandit — в JSON для компактного вывода.
LINTERS: dict[str, list[str]] = {
    "ruff": ["ruff", "check", "."],
    "ruff-format": ["ruff", "format", "--check", "."],
    "pyright": ["pyright"],
    "bandit": [
        "bandit",
        "-r",
        ".",
        "-x",
        "./migrations,./smoke_test.py,./smoke_test_ai.py",
        "-q",
        "-f",
        "json",
    ],
    "pip-audit": ["pip-audit", "-r", "requirements.txt"],
}


def execute(argv: list[str]) -> tuple[int, str, str]:
    """Запускает команду без shell; возвращает (код, stdout, stderr)."""
    env = {**os.environ, "PYTHONUTF8": "1"}
    proc = subprocess.run(  # nosec B603  # noqa: S603 - фиксированная команда, без shell
        argv,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def save_log(name: str, text: str) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    (LOG_DIR / f"{name}.log").write_text(text, encoding="utf-8")


def run_one(script: Path) -> bool:
    code, stdout, stderr = execute([sys.executable, str(script)])
    output = stdout + stderr
    save_log(script.stem, output)

    lines = output.splitlines()
    summary = [ln for ln in lines if ln.startswith("ИТОГО")]
    failed = [ln for ln in lines if ln.startswith("FAIL")]
    ok = code == 0 and not failed

    print(
        f"{'OK  ' if ok else 'FAIL'} {script.name}: {summary[-1] if summary else 'нет строки ИТОГО'}"
    )
    for ln in failed:
        print(f"   {ln}")
    if not summary:  # упал до итога — нужен traceback
        for ln in lines[-TAIL_LINES:]:
            print(f"   | {ln}")
    return ok


def format_bandit(stdout: str) -> tuple[bool, str, list[str]]:
    """Разбирает JSON bandit: (нет находок, итог, строки находок «SEV ID файл:строка текст»)."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return (
            False,
            "не удалось разобрать вывод bandit (см. лог)",
            stdout.splitlines()[-TAIL_LINES:],
        )
    results = data.get("results", [])
    errors = data.get("errors", [])
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    results.sort(key=lambda r: (order.get(r["issue_severity"], 3), r["filename"], r["line_number"]))
    findings = [
        f"{r['issue_severity']:<6} {r['test_id']} "
        f"{r['filename'].replace(os.sep, '/').removeprefix('./')}:{r['line_number']} {r['issue_text']}"
        for r in results
    ]
    counts = {sev: sum(1 for r in results if r["issue_severity"] == sev) for sev in order}
    summary = f"находок: {len(results)} (HIGH {counts['HIGH']}, MEDIUM {counts['MEDIUM']}, LOW {counts['LOW']})"
    if errors:
        summary += f"; ошибки анализа: {len(errors)}"
        findings += [f"ERROR {e}" for e in errors]
    return not results and not errors, summary, findings


def run_lint(name: str) -> bool:
    code, stdout, stderr = execute(LINTERS[name])
    save_log(f"lint_{name}", stdout + stderr)

    if name == "bandit":
        clean, summary, findings = format_bandit(stdout)
        ok = clean and code == 0
        if not ok and code not in (0, 1):  # 1 у bandit = «есть находки»; прочее — сбой запуска
            findings += stderr.splitlines()[-TAIL_LINES:]
    else:
        ok = code == 0
        lines = [ln for ln in (stdout + stderr).splitlines() if ln.strip()]
        summary = lines[-1] if lines else "нет вывода"
        findings = [] if ok else lines[:MAX_FINDINGS]

    print(f"{'OK  ' if ok else 'FAIL'} {name}: {summary[:LINE_WIDTH]}")
    for ln in findings[:MAX_FINDINGS]:
        print(f"   {ln[:LINE_WIDTH]}")
    if len(findings) > MAX_FINDINGS:
        print(
            f"   ... ещё {len(findings) - MAX_FINDINGS} (полный вывод: .test_logs/lint_{name}.log)"
        )
    return ok


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]  # кириллица на Windows-консоли
    args = sys.argv[1:]

    if args and args[0] == "lint":
        filters = args[1:]
        names = [n for n in LINTERS if not filters or any(f in n for f in filters)]
        if not names:
            print(f"Нет подходящих линтеров: {', '.join(LINTERS)}")
            return 2
        results = [run_lint(n) for n in names]
        print(
            f"\nИнструментов: {len(results)}, с замечаниями: {results.count(False)}  (полные логи: .test_logs/)"
        )
        return 0 if all(results) else 1

    scripts = sorted(ROOT.glob("smoke_test*.py"))
    if args:
        scripts = [s for s in scripts if any(f in s.name for f in args)]
    if not scripts:
        print("Нет подходящих smoke_test*.py")
        return 2
    results = [run_one(s) for s in scripts]
    print(f"\nСкриптов: {len(results)}, упало: {results.count(False)}  (полные логи: .test_logs/)")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
