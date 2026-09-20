"""Тихий запуск smoke-тестов: экономия токенов при разработке (без потери диагностики).

Печатает на каждый скрипт только строку «ИТОГО» и упавшие проверки (`FAIL ...`);
если скрипт упал без итога (traceback) — последние строки вывода. Полный вывод
каждого прогона сохраняется в `.test_logs/<имя>.log` — открывать, только если нужен контекст.

    python scripts/run_checks.py                 # все smoke_test*.py
    python scripts/run_checks.py stage4 stage5   # по подстроке имени
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 - запуск собственных тестов проекта
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / ".test_logs"
TAIL_LINES = 25


def run_one(script: Path) -> bool:
    env = {**os.environ, "PYTHONUTF8": "1"}
    proc = subprocess.run(  # nosec B603  # noqa: S603 - фиксированная команда, без shell
        [sys.executable, str(script)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = proc.stdout + proc.stderr
    LOG_DIR.mkdir(exist_ok=True)
    (LOG_DIR / f"{script.stem}.log").write_text(output, encoding="utf-8")

    lines = output.splitlines()
    summary = [ln for ln in lines if ln.startswith("ИТОГО")]
    failed = [ln for ln in lines if ln.startswith("FAIL")]
    ok = proc.returncode == 0 and not failed

    print(
        f"{'OK  ' if ok else 'FAIL'} {script.name}: {summary[-1] if summary else 'нет строки ИТОГО'}"
    )
    for ln in failed:
        print(f"   {ln}")
    if not summary:  # упал до итога — нужен traceback
        for ln in lines[-TAIL_LINES:]:
            print(f"   | {ln}")
    return ok


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]  # кириллица на Windows-консоли
    filters = sys.argv[1:]
    scripts = sorted(ROOT.glob("smoke_test*.py"))
    if filters:
        scripts = [s for s in scripts if any(f in s.name for f in filters)]
    if not scripts:
        print("Нет подходящих smoke_test*.py")
        return 2
    results = [run_one(s) for s in scripts]
    print(f"\nСкриптов: {len(results)}, упало: {results.count(False)}  (полные логи: .test_logs/)")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
