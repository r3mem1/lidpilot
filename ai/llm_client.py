"""
Клиент LLM API — раздел 9 ТЗ («API выбранной LLM»).

Основной провайдер — OpenRouter (AI_PROVIDER=openrouter: нужны только AI_API_KEY
и AI_MODEL), но клиент работает с любым сервисом, совместимым с OpenAI
/chat/completions (OpenAI, DeepSeek, Together, локальный vLLM): адрес, модель
и ключ задаются в .env. Для другого протокола
достаточно добавить реализацию протокола LLMClient — остальной AI-модуль
об этом не знает.

Ключ API читается только из окружения и никогда не логируется (раздел 16).

Ошибки внешнего API не приводят к потере обращения: клиент делает ограниченное
число повторов на таймаутах/5xx/429, а затем бросает LLMUnavailable, по которой
pipeline передаёт диалог менеджеру (раздел 6.7).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx

from config import settings

logger = logging.getLogger("leadpilot.ai.llm")

Purpose = Literal["classify", "respond"]


class LLMError(Exception):
    """Базовая ошибка AI-модуля."""


class LLMUnavailable(LLMError):
    """Сервис недоступен: таймаут, сетевая ошибка, 5xx, 429, отказ авторизации."""


class LLMInvalidResponse(LLMError):
    """Ответ получен, но это не валидный JSON ожидаемой структуры."""


@dataclass(frozen=True)
class LLMResult:
    data: dict
    raw: str
    model: str
    latency_ms: int
    attempts: int


class LLMClient(Protocol):
    """Контракт клиента LLM.

    offline=True означает, что реальных вызовов нет и AI-модуль должен работать
    детерминированным путём (правила + шаблон по данным БД).
    """

    offline: bool

    def complete_json(
        self, messages: list[dict[str, str]], *, purpose: Purpose, model: str | None = None
    ) -> LLMResult: ...


# --------------------------------------------------------------------------- #
# Разбор JSON из ответа модели
# --------------------------------------------------------------------------- #
_FENCE_RE = re.compile(r"^```(?:json)?|```$", re.MULTILINE)


def extract_json_object(text: str) -> dict:
    """Достаёт JSON-объект из ответа модели.

    Модели периодически оборачивают JSON в ```json ... ``` или добавляют текст
    до/после, поэтому кроме прямого разбора ищем первый сбалансированный
    объект. Если объекта нет — это LLMInvalidResponse, а не «пустой ответ»:
    выдумывать вместо модели нельзя.
    """
    cleaned = _FENCE_RE.sub("", text or "").strip()
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    while start != -1:
        depth, in_string, escaped = 0, False, False
        for index in range(start, len(cleaned)):
            char = cleaned[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = cleaned[start : index + 1]
                    try:
                        parsed = json.loads(candidate)
                    except json.JSONDecodeError:
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break
        start = cleaned.find("{", start + 1)

    raise LLMInvalidResponse("В ответе модели нет JSON-объекта")


# --------------------------------------------------------------------------- #
# Офлайн-режим (AI_PROVIDER=stub)
# --------------------------------------------------------------------------- #
class OfflineLLMClient:
    """Локальная разработка и тесты без ключа API.

    Вызовов не делает: классификация идёт по правилам, ответ собирается
    шаблоном строго из данных компании. В production такой режим запрещён
    (проверка конфигурации при старте).
    """

    offline = True

    def complete_json(
        self, messages: list[dict[str, str]], *, purpose: Purpose, model: str | None = None
    ) -> LLMResult:
        raise LLMUnavailable("AI_PROVIDER=stub: обращения к LLM отключены")


# --------------------------------------------------------------------------- #
# OpenAI-совместимый провайдер
# --------------------------------------------------------------------------- #
class OpenAICompatibleLLMClient:
    """Клиент для сервисов с эндпоинтом POST {base_url}/chat/completions."""

    offline = False
    _RETRY_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        temperature: float | None = None,
    ) -> None:
        self._base_url = (base_url or settings.ai_api_base_url).rstrip("/")
        self._api_key = api_key or settings.ai_api_key or ""
        self._model = model or settings.ai_model or ""
        self._timeout = timeout if timeout is not None else settings.ai_timeout_seconds
        self._max_retries = max_retries if max_retries is not None else settings.ai_max_retries
        self._temperature = temperature if temperature is not None else settings.ai_temperature

    def complete_json(
        self, messages: list[dict[str, str]], *, purpose: Purpose, model: str | None = None
    ) -> LLMResult:
        payload = {
            "model": model or self._model,
            "messages": messages,
            "temperature": self._temperature,
            # Поддерживается не всеми провайдерами; при отказе повторяем без него.
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if "openrouter.ai" in self._base_url:
            # Необязательные заголовки OpenRouter: имя приложения в его статистике.
            headers["X-Title"] = settings.app_name

        started = time.monotonic()
        last_error: Exception | None = None
        allow_response_format = True

        attempt = 0
        while attempt < self._max_retries + 1:
            body = dict(payload)
            if not allow_response_format:
                body.pop("response_format", None)
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    response = client.post(
                        f"{self._base_url}/chat/completions", json=body, headers=headers
                    )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                attempt += 1
                last_error = exc
                logger.warning("LLM %s: сетевая ошибка (попытка %s): %s", purpose, attempt, exc)
                self._sleep_before_retry(attempt)
                continue

            if response.status_code == 400 and allow_response_format:
                # Провайдер не знает response_format — повторяем без него.
                # Это не «попытка»: иначе при AI_MAX_RETRIES=0 повтор не случился бы.
                allow_response_format = False
                logger.info("LLM %s: провайдер не поддерживает response_format", purpose)
                continue

            attempt += 1
            if response.status_code in self._RETRY_STATUSES:
                last_error = LLMUnavailable(f"HTTP {response.status_code}")
                logger.warning(
                    "LLM %s: HTTP %s (попытка %s)", purpose, response.status_code, attempt
                )
                self._sleep_before_retry(attempt)
                continue

            if response.status_code >= 400:
                # 401/402/403/404 — ошибка конфигурации (ключ, баланс, id модели),
                # повторять бессмысленно. Короткий фрагмент ответа провайдера
                # ускоряет диагностику; ключа в нём нет.
                raise LLMUnavailable(
                    f"LLM вернул HTTP {response.status_code}: {response.text[:200]!r}"
                )

            latency_ms = int((time.monotonic() - started) * 1000)
            try:
                response_data = response.json()
            except ValueError as exc:
                raise LLMInvalidResponse("Ответ LLM не является JSON") from exc
            text = self._extract_message_text(response_data)
            return LLMResult(
                data=extract_json_object(text),
                raw=text,
                model=body["model"],
                latency_ms=latency_ms,
                attempts=attempt,
            )

        raise LLMUnavailable(f"LLM недоступен после {self._max_retries + 1} попыток: {last_error}")

    def _sleep_before_retry(self, attempt: int) -> None:
        if attempt <= self._max_retries:
            time.sleep(min(0.5 * (2 ** (attempt - 1)), 4.0))

    @staticmethod
    def _extract_message_text(data: dict) -> str:
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMInvalidResponse("Неожиданная структура ответа LLM") from exc


# --------------------------------------------------------------------------- #
# Фабрика
# --------------------------------------------------------------------------- #
def get_llm_client() -> LLMClient:
    """Клиент по текущей конфигурации (раздел 16: параметры только из .env)."""
    if settings.ai_provider in ("openrouter", "openai_compatible"):
        return OpenAICompatibleLLMClient()
    logger.warning("AI_PROVIDER=stub — AI работает в офлайн-режиме (только для разработки)")
    return OfflineLLMClient()
