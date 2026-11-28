"""Різноманітні відповіді асистента.

Джерела варіантів для кожної фрази (ключа):
  1) phrases.json — ваші варіанти (замінюють вбудовані для цього ключа);
  2) вбудовані варіанти (DEFAULTS);
  3) варіанти, які заздалегідь згенерував ШІ (phrases_ai.json) — додаються до пулу.
Фраза вибирається випадково, але не повторює попередню для того самого ключа.
ШІ працює у фоні й кешує результат, тож голосова відповідь лишається миттєвою.

У фразах можна використовувати плейсхолдери: {name} — ім'я асистента, {user} — ім'я користувача,
та специфічні для ключа (напр. {duration}, {time}). Їх треба зберігати без змін.
У commands.json поле "speak" може бути рядком або списком варіантів."""
import hashlib
import json
import logging
import random
import re
import threading
import time

import common
from ai import AIError, generate_text

log = logging.getLogger("asistent")

PLACEHOLDER = re.compile(r"\{(\w+)\}")
MAX_VARIANT_LEN = 120
KEYS_PER_REQUEST = 10          # скільки ключів просимо в ШІ за один запит

DEFAULTS = {
    "greeting": ["{name}... Вітає вас!", "Привіт, {user}! Я {name}, готова до роботи.",
                 "{name} на зв'язку. Чим можу допомогти?"],
    "listening": ["Слухаю вас.", "Слухаю.", "Так, {user}?", "Я вся в увазі."],
    "not_found": ["Команду не знайдено.", "Не зрозумла команду.", "Такої команди я не знаю."],
    "error": ["Помилка виконання.", "Щось пішло не так.", "Не вдалося це виконати."],
    "goodbye": ["Прощавайте...!", "До зустрічі, {user}!", "Бувайте!"],
    "dialog_start": ["Добре, {user}, слухаю вас.", "Давайте поговоримо.", "Про що поговоримо?"],
    "dialog_end": ["Завершую діалог.", "Гаразд, закінчили розмову.", "Добре, діалог завершено."],
    "no_api_key": ["У налаштуваннях не вказано API ключ. Додайте його, щоб я міг відповідати.",
                   "Мені потрібен API ключ, щоб відповідати. Додайте його в налаштуваннях."],
    "no_api_key_dialog": ["У налаштуваннях не вказано API ключ. Не можу почати діалог.",
                          "Без API ключа я не можу розмовляти. Додайте його в налаштуваннях."],
    "time_now": ["Зараз {time}.", "Час — {time}.", "На годиннику {time}."],
    "date_today": ["Сьогодні {date}.", "Сьогодні у нас {date}."],
    "datetime_both": ["Зараз {time}, сьогодні {date}.", "Зараз {time}. Сьогодні {date}."],
    "timer_set": ["Таймер на {duration} запущено.", "Поставив таймер на {duration}.", "Засік {duration}."],
    "timer_done": ["Час вийшов! Минуло {duration}.", "Таймер спрацював: минуло {duration}."],
    "timer_done_label": ["Час вийшов! Таймер {label} завершено.", "Таймер {label} спрацював."],
    "timers_cancelled": ["Таймери скасовано.", "Усі таймери скасовано."],
    "timers_none": ["Запущених таймерів немає.", "Активних таймерів не знайшов."],
    "volume_up": ["Гучність збільшено.", "Зробила голосніше."],
    "volume_down": ["Гучність зменшено.", "Зробила тихіше."],
    "volume_mute": ["Звук вимкнено (або увімкнено знову).", "Перемкнув звук."],
    "volume_set": ["Гучність {percent} відсотків.", "Виставив гучність на {percent} відсотків."],
    "note_saved": ["Нотатку записано.", "Записав.", "Готово, нотатку збережено."],
    "notes_none": ["Нотаток ще немає.", "Поки що жодної нотатки."],
    "notes_read": ["Останні нотатки: {notes}", "Ось що я записав: {notes}"],
    "searching": ["Шукаю: {query}.", "Зараз знайду: {query}."],
    "hint_timer": ["Скажіть, на скільки поставити таймер.", "На який час поставити таймер?"],
    "hint_volume": ["Скажіть, що зробити з гучністю: вище, нижче чи конкретне число.",
                    "Що зробити з гучністю: збільшити, зменшити чи виставити число?"],
    "hint_note": ["Що саме записати?", "Що занести в нотатку?"],
    "hint_search": ["Що саме знайти?", "Що шукати?"],
}

DEFAULT_STYLE = "дружній і природний, іноді з легким гумором; коротко"


def _placeholders(text):
    return set(PLACEHOLDER.findall(text))


def _as_list(value):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if isinstance(v, str) and v.strip()]


def write_default(path):
    """Створює phrases.json з вбудованими варіантами — зручна заготовка для редагування."""
    common.save_json_file(path, DEFAULTS)


class Phrases:
    def __init__(self, file_path, cache_path):
        self.file_path = file_path
        self.cache_path = cache_path
        self.context = {"name": "Норі", "user": "друже"}
        self.style = DEFAULT_STYLE
        self.use_ai = True
        self._file = {}                  # ключ -> [варіанти] з phrases.json
        self._cache = {}                 # ключ -> {"seed": хеш, "ts": час, "variants": [...]}
        self._commands = {}              # "cmd:<id>" -> [варіанти speak із commands.json]
        self._no_ai = set()              # команди з "ai_variants": false — лише ваші варіанти
        self._last = {}
        self._lock = threading.RLock()
        self._gen_lock = threading.Lock()

    # ── завантаження ─────────────────────────────────────────────────────────
    def configure(self, name=None, user=None, style=None, use_ai=None):
        if name:
            self.context["name"] = name
        if user:
            self.context["user"] = user
        if style is not None:
            self.style = style.strip() or DEFAULT_STYLE
        if use_ai is not None:
            self.use_ai = bool(use_ai)

    def reload(self):
        data = common.read_json(self.file_path) or {}
        cache = common.read_json(self.cache_path) or {}
        with self._lock:
            self._file = {k: v for k, v in ((k, _as_list(v)) for k, v in data.items()) if v}
            keys = cache.get("keys", {}) if isinstance(cache.get("keys"), dict) else {}
            self._cache = {k: e for k, e in keys.items()
                           if isinstance(e, dict) and isinstance(e.get("variants"), list)}

    def set_commands(self, commands):
        """Запам'ятовує варіанти "speak" користувацьких команд (рядок або список)."""
        found, no_ai = {}, set()
        for cmd_id, info in (commands or {}).items():
            if isinstance(info, dict) and not info.get("disabled"):
                variants = _as_list(info.get("speak"))
                if variants:
                    found[f"cmd:{cmd_id}"] = variants
                    if info.get("ai_variants") is False:
                        no_ai.add(f"cmd:{cmd_id}")
        with self._lock:
            self._commands, self._no_ai = found, no_ai

    # ── пул і вибір ──────────────────────────────────────────────────────────
    def base_variants(self, key):
        with self._lock:
            if key in self._commands:
                return list(self._commands[key])
            return list(self._file.get(key) or DEFAULTS.get(key, []))

    def _seed_hash(self, variants):
        return hashlib.sha1(json.dumps([variants, self.style], ensure_ascii=False).encode("utf-8")).hexdigest()[:16]

    def ai_variants(self, key):
        """Варіанти ШІ, але лише якщо вони згенеровані для актуальних еталонів і стилю."""
        if not self.use_ai or key in self._no_ai:
            return []
        with self._lock:
            entry = self._cache.get(key)
            base = self.base_variants(key)
            if not entry or not base or entry.get("seed") != self._seed_hash(base):
                return []
            return _as_list(entry["variants"])

    def pool(self, key):
        base = self.base_variants(key)
        seen, out = set(), []
        for v in base + self.ai_variants(key):
            if v.lower() not in seen:
                seen.add(v.lower())
                out.append(v)
        return out

    def _choose(self, key, pool):
        if not pool:
            return ""
        with self._lock:
            options = [v for v in pool if v != self._last.get(key)] or pool
            choice = random.choice(options)
            self._last[key] = choice
            return choice

    def _format(self, text, fmt):
        values = {**self.context, **fmt}
        try:
            return text.format(**values)
        except (KeyError, IndexError, ValueError):
            return None

    def get(self, key, **fmt):
        """Випадковий варіант фрази з підставленими плейсхолдерами. '' якщо такої фрази немає."""
        text = self._format(self._choose(key, self.pool(key)), fmt)
        if text is not None:
            return text
        for fallback in DEFAULTS.get(key, []):         # помилка в користувацькому варіанті
            text = self._format(fallback, fmt)
            if text is not None:
                log.info(f"[ФРАЗИ] Варіант для '{key}' містить невідомий плейсхолдер — беру вбудований.")
                return text
        return ""

    def for_command(self, cmd_id, speak_value):
        """Відповідь користувацької команди: speak — рядок або список; плюс варіанти ШІ."""
        variants = _as_list(speak_value)
        if not variants:
            return ""
        key = f"cmd:{cmd_id}"
        with self._lock:
            self._commands.setdefault(key, variants)
        text = self._format(self._choose(key, self.pool(key)), {})
        return text if text is not None else variants[0]

    # ── генерація варіантів через ШІ ─────────────────────────────────────────
    def seeds(self):
        keys = {k: self.base_variants(k) for k in DEFAULTS}
        with self._lock:
            keys.update({k: list(v) for k, v in self._commands.items()})
        return {k: v for k, v in keys.items() if v and k not in self._no_ai}

    def stale_keys(self, force=False, refresh_days=7):
        """Ключі, для яких потрібні нові варіанти: немає кешу, змінились еталони/стиль, кеш застарів."""
        now, stale = time.time(), []
        for key, base in self.seeds().items():
            entry = self._cache.get(key)
            if (force or not entry or entry.get("seed") != self._seed_hash(base)
                    or now - float(entry.get("ts", 0)) > refresh_days * 86400):
                stale.append(key)
        return stale

    def _prune_cache(self):
        valid = set(self.seeds())
        with self._lock:
            for key in [k for k in self._cache if k not in valid]:
                del self._cache[key]

    @staticmethod
    def _parse_json(text):
        text = text.strip()
        try:
            return json.loads(text)
        except ValueError:
            m = re.search(r"\{.*\}", text, re.S)
            if m:
                try:
                    return json.loads(m.group(0))
                except ValueError:
                    pass
        return {}

    @staticmethod
    def _clean(seed_variants, candidates, limit):
        """Відсіює варіанти ШІ: ті самі плейсхолдери, розумна довжина, без дублів і без еталонів."""
        union = set().union(*(_placeholders(v) for v in seed_variants))
        required = set.intersection(*(_placeholders(v) for v in seed_variants))
        seen = {v.lower() for v in seed_variants}
        out = []
        for cand in _as_list(candidates):
            cand = cand.strip(" \"'«»")
            ph = _placeholders(cand)
            if not cand or len(cand) > MAX_VARIANT_LEN or "\n" in cand or cand.lower() in seen:
                continue
            if not required <= ph <= union:
                continue
            try:
                cand.format(**{p: "x" for p in union})
            except (KeyError, IndexError, ValueError):
                continue
            seen.add(cand.lower())
            out.append(cand)
            if len(out) >= limit:
                break
        return out

    def _prompt(self, batch, per_key):
        return (
            "Ти допомагаєш українському голосовому асистентові звучати природно й різноманітно.\n"
            f"Асистента звати {self.context['name']}, користувача — {self.context['user']}.\n"
            "Нижче JSON: ключ -> еталонні фрази. Для КОЖНОГО ключа придумай "
            f"{per_key} нових варіантів зі ТИМ САМИМ змістом, але іншими словами.\n"
            f"Стиль: {self.style}.\n"
            "Правила: зберігай усі плейсхолдери у фігурних дужках (напр. {duration}) точно як в еталоні, "
            "нових не додавай; не вигадуй фактів; кожен варіант до 90 символів; без емодзі, markdown і лапок; "
            "не повторюй еталонні фрази; варіанти мають бути придатні для озвучення голосом.\n"
            "Поверни ЛИШЕ JSON-об'єкт: {\"ключ\": [\"варіант\", ...], ...}.\n\n"
            + json.dumps(batch, ensure_ascii=False))

    def generate(self, api_key, model, force=False, per_key=6, refresh_days=7):
        """Просить ШІ дописати варіанти й зберігає їх у phrases_ai.json. Блокує потік, тож
        викликається у фоні. Повертає кількість доданих варіантів (0, якщо оновлювати нічого або вже йде генерація)."""
        if not api_key or not self.use_ai:
            return 0
        if not self._gen_lock.acquire(blocking=False):
            return 0
        try:
            self._prune_cache()
            stale = self.stale_keys(force, refresh_days)
            if not stale:
                return 0
            seeds, added = self.seeds(), 0
            log.info(f"[ФРАЗИ] Генерую варіанти через ШІ для {len(stale)} фраз...")
            for i in range(0, len(stale), KEYS_PER_REQUEST):
                keys = stale[i:i + KEYS_PER_REQUEST]
                batch = {k: seeds[k] for k in keys}
                try:
                    raw = generate_text(api_key, model, self._prompt(batch, per_key))
                except AIError as e:
                    log.info(f"[ФРАЗИ] ШІ не відповів: {e.user_message}")
                    break
                data = self._parse_json(raw)
                for key in keys:
                    variants = self._clean(seeds[key], (data.get(key) if isinstance(data, dict) else None), per_key)
                    if variants:
                        with self._lock:
                            self._cache[key] = {"seed": self._seed_hash(seeds[key]), "ts": time.time(),
                                                "variants": variants}
                        added += len(variants)
            with self._lock:
                payload = {"meta": {"model": model, "style": self.style, "updated": int(time.time())},
                           "keys": self._cache}
            common.save_json_file(self.cache_path, payload)
            log.info(f"[ФРАЗИ] Додано варіантів від ШІ: {added}")
            return added
        finally:
            self._gen_lock.release()
