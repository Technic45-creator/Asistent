"""Спільний код асистента і GUI: шляхи, JSON-файли, логування, збереження API-ключа."""
import json
import logging
import logging.handlers
import os
import shutil
import sys

# ─── Папки ────────────────────────────────────────────────────────────────────
# BASE_DIR  — поруч з exe/скриптом: тут лежать comand.json, user.dat, seting.json, model_uk, лог.
# RES_DIR   — тимчасова папка PyInstaller (_MEIPASS) із вбудованими ресурсами.
# ASISTENT_HOME — необов'язкова змінна середовища: переносить усі дані в іншу папку.
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    RES_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    RES_DIR = BASE_DIR
if os.environ.get("ASISTENT_HOME"):
    BASE_DIR = os.path.abspath(os.environ["ASISTENT_HOME"])

KEYRING_SERVICE = "Asistent"
KEYRING_USER = "gemini_api_key"


def path_of(name):
    return os.path.join(BASE_DIR, name)


def commands_file():
    return path_of("comand.json")


def user_file():
    return path_of("user.dat")


def settings_file():
    return path_of("seting.json")


def find_file(name):
    """Спершу поруч з exe, потім серед вбудованих ресурсів, потім у поточній теці."""
    for d in (BASE_DIR, RES_DIR, os.getcwd()):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


def resource_path(name):
    """Шукає ресурс (іконку) поруч із програмою та у відомих підтеках."""
    candidates = [
        os.path.join(BASE_DIR, name),
        os.path.join(RES_DIR, name),
        os.path.join(BASE_DIR, "Asistent", "AsistentGUI", name),
        os.path.join(os.getcwd(), name),
        os.path.join(os.getcwd(), "Asistent", "AsistentGUI", name),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return candidates[0]


# ─── Значення за замовчуванням для налаштувань асистента (seting.json) ────────
ASSISTANT_DEFAULTS = {
    "assistant_name": "Норі",
    "volume": 100,                 # гучність мовлення, 0..100
    "tts_model": "",               # назва голосу SAPI (порожньо = автоматично український)
    "tts_model_id": "",
    "mute_microphone": False,
    "hotkey": "f10",               # активація
    "stop_hotkey": "f9",           # перервати мовлення і відповідь AI
    "tts_rate": 2,                 # швидкість SAPI: -10..10
    "tts_backend": "sapi",         # "sapi" (офлайн) або "edge" (онлайн, якісніший голос)
    "edge_voice": "uk-UA-PolinaNeural",   # або uk-UA-OstapNeural
    "edge_rate": 0,                # % швидкості для edge
    "active_timeout": 10,          # секунд чекати команду після активації
    "echo_tail": 0.3,              # пауза після мовлення, щоб мікрофон не зловив відлуння
    "ai_stream": True,             # озвучувати відповідь по реченнях, поки вона приходить
    "ai_tools": True,              # дозволити AI виконувати команди (function calling)
    "ai_fallback": True,           # невпізнану команду віддавати AI
    "ai_thinking_budget": None,    # int або null
    "builtins": True,              # вбудовані команди: час, таймер, гучність, нотатки, пошук
    "dialog_stt": "vosk",          # "vosk" або "whisper" (точніше, але повільніше)
    "whisper_model": "small",
    "wake_grammar": False,         # експеримент: слово активації через вузьку граматику Vosk
    "wake_words_extra": [],        # додаткові слова активації
    "phrases_ai": True,            # ШІ заздалегідь дописує варіанти фраз (кеш phrases_ai.json)
    "phrases_style": "",            # стиль фраз для ШІ; порожньо = дружній, коротко
    "phrases_ai_variants": 6,      # скільки нових варіантів на фразу
    "phrases_ai_refresh_days": 7,  # як часто оновлювати варіанти
}

# ─── Прочитані файли ──────────────────────────────────────────────────────────
LOAD_ERRORS = []     # помилки читання; GUI показує їх у журналі


def backup_broken_file(path, error):
    """Копія нечитабельного файлу (*.bad): наступне збереження не знищить дані."""
    bad = path + ".bad"
    try:
        shutil.copy2(path, bad)
        note = f"копію збережено: {bad}"
    except OSError:
        note = "копію зберегти не вдалося"
    LOAD_ERRORS.append(f"[ПОМИЛКА] Не вдалося прочитати {os.path.basename(path)}: {error} ({note})")


def read_json(path):
    """dict або None (файлу немає, він пошкоджений чи не є JSON-об'єктом)."""
    if not os.path.isfile(path):
        return None
    try:
        # utf-8-sig: Блокнот Windows зберігає файл з BOM
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("очікувався JSON-об'єкт {...}")
        return data
    except Exception as e:
        backup_broken_file(path, e)
        return None


def load_json_file(path, defaults=None):
    data = dict(defaults or {})
    loaded = read_json(path)
    if loaded:
        data.update(loaded)     # невідомі ключі зберігаються
    return data


def save_json_file(path, data):
    """Атомарний запис: інша програма ніколи не побачить наполовину записаний файл."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ─── API-ключ: Диспетчер облікових даних Windows (keyring), якщо встановлений ─
def _keyring():
    try:
        import keyring
        return keyring
    except Exception:
        return None


def get_api_key(user_data=None):
    kr = _keyring()
    if kr:
        try:
            value = kr.get_password(KEYRING_SERVICE, KEYRING_USER)
            if value:
                return value
        except Exception:
            pass
    return ((user_data or {}).get("api_key") or "").strip()


def _store_api_key(key):
    """True, якщо ключ тепер зберігається в keyring (і його не треба писати у файл)."""
    kr = _keyring()
    if not kr:
        return False
    try:
        if key:
            kr.set_password(KEYRING_SERVICE, KEYRING_USER, key)
            return True
        try:
            kr.delete_password(KEYRING_SERVICE, KEYRING_USER)
        except Exception:
            pass
        return True
    except Exception:
        return False


def load_user():
    """user.dat + ключ з keyring (якщо є)."""
    data = load_json_file(user_file())
    data["api_key"] = get_api_key(data)
    return data


def save_user(data):
    out = dict(data)
    key = (out.get("api_key") or "").strip()
    if _store_api_key(key):
        out["api_key"] = ""          # у файлі ключа немає — він у сховищі Windows
    save_json_file(user_file(), out)


# ─── Логування: консоль (її читає GUI) + файл з ротацією ──────────────────────
def setup_logging(name="asistent"):
    """Повідомлення мають вигляд "[ТЕГ] текст". У консоль — як є, у файл — з часом."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        fh = logging.handlers.RotatingFileHandler(
            path_of(f"{name}.log"), maxBytes=512 * 1024, backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(fh)
    except OSError:
        pass
    if sys.stdout is not None:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(ch)
    return logger
