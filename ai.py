"""Gemini: діалог з історією, потокова відповідь (озвучуємо по реченнях),
виклик функцій (AI сам виконує команди), перевірка ключа і список моделей."""
import json
import logging
import threading

import requests

from uktext import pop_sentences

log = logging.getLogger("asistent")

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-flash-latest"
MAX_HISTORY = 24          # скільки останніх повідомлень тримати в контексті
MAX_TOKENS = 1024         # з запасом: токени «мислення» входять у ліміт
TIMEOUT = (5, 64)        # (з'єднання, очікування наступного шматка), секунд
MAX_TOOL_ROUNDS = 3


class AIError(Exception):
    """Помилка з готовим текстом, який можна озвучити користувачу."""

    def __init__(self, user_message, detail=""):
        super().__init__(detail or user_message)
        self.user_message = user_message


def http_error_message(code):
    if code == 429:
        return "Перевищено ліміт запитів до моделі. Спробуйте трохи пізніше."
    if code in (400, 401, 403):
        return "Сервер відхилив запит. Перевірте API ключ у налаштуваннях."
    if code == 404:
        return "Модель не знайдено. Перевірте назву моделі в налаштуваннях."
    return "Виникла помилка на сервері ШІ."


def _headers(key):
    return {"x-goog-api-key": key, "Content-Type": "application/json"}


def list_models(key, timeout=10):
    """Назви моделей, що вміють generateContent. AIError, якщо ключ не підходить."""
    if not key:
        raise AIError("API ключ не вказано.")
    names, token = [], None
    try:
        for _ in range(5):
            params = {"pageSize": 100}
            if token:
                params["pageToken"] = token
            r = requests.get(f"{API_ROOT}/models", headers=_headers(key), params=params, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            for m in data.get("models", []):
                if "generateContent" in m.get("supportedGenerationMethods", []):
                    names.append(m.get("name", "").removeprefix("models/"))
            token = data.get("nextPageToken")
            if not token:
                break
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else 0
        raise AIError(http_error_message(code), f"HTTP {code}")
    except requests.exceptions.Timeout:
        raise AIError("Сервер довго не відповідає.")
    except requests.exceptions.RequestException as e:
        raise AIError("Виникла помилка зв'язку з сервером.", str(e))
    return sorted(n for n in names if n)


def generate_text(key, model, prompt, timeout=60, max_tokens=8192, temperature=1.0, json_mode=True):
    """Разовий запит без історії й без озвучення (напр. для генерації варіантів фраз). Повертає текст."""
    if not key:
        raise AIError("API ключ не вказано.")
    config = {"maxOutputTokens": max_tokens, "temperature": temperature}
    if json_mode:
        config["responseMimeType"] = "application/json"
    try:
        r = requests.post(f"{API_ROOT}/models/{model or DEFAULT_MODEL}:generateContent",
                          headers=_headers(key), timeout=timeout,
                          json={"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                                "generationConfig": config})
        r.raise_for_status()
        cands = r.json().get("candidates") or []
        parts = cands[0].get("content", {}).get("parts", []) if cands else []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else 0
        raise AIError(http_error_message(code), f"HTTP {code}")
    except requests.exceptions.Timeout:
        raise AIError("Сервер довго не відповідає.")
    except requests.exceptions.RequestException as e:
        raise AIError("Виникла помилка зв'язку з сервером.", str(e))
    if not text:
        raise AIError("Модель повернула порожню відповідь.")
    return text


class Gemini:
    def __init__(self, cfg, get_key, get_model, system_prompt, tool_decls=None, run_tool=None):
        """cfg(key, default) — налаштування; get_key()/get_model() — поточні значення;
        system_prompt() -> str; tool_decls() -> список описів функцій; run_tool(name, args) -> str."""
        self.cfg = cfg
        self.get_key = get_key
        self.get_model = get_model
        self.system_prompt = system_prompt
        self.tool_decls = tool_decls
        self.run_tool = run_tool
        self.history = []
        self._cancel = threading.Event()
        self._tools_broken = False

    def cancel(self):
        self._cancel.set()

    def reset(self):
        self.history.clear()

    # ── історія ──────────────────────────────────────────────────────────────
    @staticmethod
    def _is_plain_user(msg):
        parts = msg.get("parts") or [{}]
        return msg.get("role") == "user" and "text" in parts[0]

    def _trim(self):
        """Історія завжди починається зі звичайного повідомлення користувача (не з відповіді функції)."""
        while self.history and (len(self.history) > MAX_HISTORY or not self._is_plain_user(self.history[0])):
            self.history.pop(0)

    def _repair_tail(self, reply):
        """Після переривання чи вичерпання раундів історія не має закінчуватися викликом без відповіді
        або відповіддю функції без підсумку — інакше наступний запит буде відхилено."""
        if not self.history:
            return
        last = self.history[-1]
        if last["role"] == "model" and any("functionCall" in p for p in last["parts"]):
            last["parts"] = [{"text": reply or "Перервано."}]      # виклик без відповіді замінюємо текстом
        elif last["role"] == "user" and not self._is_plain_user(last):
            self.history.append({"role": "model", "parts": [{"text": reply or "Готово."}]})

    # ── запит ────────────────────────────────────────────────────────────────
    def _payload(self, use_tools):
        gen = {"maxOutputTokens": MAX_TOKENS}
        budget = self.cfg("ai_thinking_budget", None)
        if isinstance(budget, int) and not isinstance(budget, bool):
            gen["thinkingConfig"] = {"thinkingBudget": budget}
        payload = {
            "system_instruction": {"parts": [{"text": self.system_prompt()}]},
            "contents": list(self.history),
            "generationConfig": gen,
        }
        if use_tools and self.tool_decls and not self._tools_broken:
            decls = self.tool_decls()
            if decls:
                payload["tools"] = [{"functionDeclarations": decls}]
        return payload

    def _post(self, payload, stream):
        method = "streamGenerateContent" if stream else "generateContent"
        r = requests.post(
            f"{API_ROOT}/models/{self.get_model() or DEFAULT_MODEL}:{method}",
            headers=_headers(self.get_key()),
            params={"alt": "sse"} if stream else None,
            json=payload, timeout=TIMEOUT, stream=stream)
        r.raise_for_status()
        return r

    @staticmethod
    def _chunks(response, stream):
        if not stream:
            yield response.json()
            return
        for raw in response.iter_lines():        # байти: SSE без charset requests прочитав би як latin-1
            if not raw:
                continue
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                yield json.loads(data)
            except ValueError:
                continue

    def _round(self, on_sentence, use_tools, allow_empty=False):
        """Один запит до моделі. Повертає (текст, [functionCall], parts_для_історії)."""
        stream = bool(self.cfg("ai_stream", True))
        payload = self._payload(use_tools)
        try:
            response = self._post(payload, stream)
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            if code == 400 and "tools" in payload:
                # деякі моделі не приймають tools — повторюємо без функцій і більше їх не шлемо
                log.info("[AI] Модель відхилила tools — вимикаю виклик функцій.")
                self._tools_broken = True
                response = self._post(self._payload(False), stream)
            else:
                raise

        text_total, buf, calls, call_parts = "", "", [], []
        finish = block = None
        for chunk in self._chunks(response, stream):
            if self._cancel.is_set():
                break
            block = chunk.get("promptFeedback", {}).get("blockReason") or block
            cands = chunk.get("candidates") or []
            if not cands:
                continue
            finish = cands[0].get("finishReason") or finish
            for part in cands[0].get("content", {}).get("parts", []):
                if part.get("thought"):
                    continue
                if "functionCall" in part:
                    calls.append(part["functionCall"])
                    call_parts.append(part)             # з thoughtSignature, якщо модель його дала
                elif part.get("text"):
                    text_total += part["text"]
                    buf += part["text"]
                    done, buf = pop_sentences(buf)
                    for sentence in done:
                        if on_sentence:
                            on_sentence(sentence)
        if buf.strip() and on_sentence and not self._cancel.is_set():
            on_sentence(buf)
        if not text_total.strip() and not calls and not allow_empty and not self._cancel.is_set():
            log.info(f"[AI] Порожня відповідь (finishReason={finish}, blockReason={block})")
            raise AIError("Я не отримав відповіді від моделі.")
        parts = ([{"text": text_total}] if text_total.strip() else []) + call_parts
        return text_total.strip(), calls, parts

    def ask(self, text, on_sentence=None, keep_history=True, use_tools=True):
        """Питання -> повна відповідь. on_sentence(речення) викликається, щойно речення готове.
        keep_history=False — разове питання, що не потрапляє в діалог."""
        if not self.get_key():
            raise AIError("У налаштуваннях не вказано API ключ. Додайте його, щоб я міг відповідати.")
        self._cancel.clear()
        start = len(self.history)
        self.history.append({"role": "user", "parts": [{"text": text}]})
        replies, tool_ran = [], False
        try:
            for _ in range(MAX_TOOL_ROUNDS + 1):
                reply, calls, parts = self._round(on_sentence, use_tools and bool(self.run_tool),
                                                  allow_empty=tool_ran)
                if reply:
                    replies.append(reply)
                if parts:                                   # модель без жодної частини в історію не кладемо
                    self.history.append({"role": "model", "parts": parts})
                if not calls or self._cancel.is_set():
                    break
                responses = []
                for call in calls:
                    name, args = call.get("name", ""), call.get("args") or {}
                    log.info(f"[AI] Виклик функції: {name}({json.dumps(args, ensure_ascii=False)})")
                    try:
                        result = str(self.run_tool(name, args))
                    except Exception as e:
                        result = f"Помилка: {e}"
                    tool_ran = True
                    responses.append({"functionResponse": {"name": name, "response": {"result": result}}})
                self.history.append({"role": "user", "parts": responses})
        except requests.exceptions.Timeout:
            del self.history[start:]
            raise AIError("Сервер довго не відповідає, спробуйте ще раз.", "timeout")
        except requests.exceptions.HTTPError as e:
            del self.history[start:]
            code = e.response.status_code if e.response is not None else 0
            body = e.response.text[:300] if e.response is not None else ""
            log.info(f"[AI] Помилка запиту: HTTP {code}: {body}")
            raise AIError(http_error_message(code), f"HTTP {code}")
        except requests.exceptions.RequestException as e:
            del self.history[start:]
            log.info(f"[AI] Помилка запиту: {e}")
            raise AIError("Виникла помилка зв'язку з сервером.", str(e))
        except Exception:
            del self.history[start:]      # ролі в історії мають чергуватися
            raise
        if not keep_history:
            del self.history[start:]
        else:
            self._repair_tail(" ".join(replies).strip())
            self._trim()
        reply = " ".join(replies).strip()
        if not reply and tool_ran:
            reply = "Готово."
            if on_sentence:
                on_sentence(reply)
        return reply
