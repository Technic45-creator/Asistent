import os
import re
import sys
import json
import shutil
import threading
import subprocess
import tempfile
import contextlib
import webbrowser
from datetime import datetime

from PyQt6 import QtCore, QtGui, QtWidgets

import ai
import common
import phrases as phrases_mod
from common import (BASE_DIR, LOAD_ERRORS, resource_path,
                    backup_broken_file, load_json_file, save_json_file)
from matching import match_commands, find_overlaps
from ui_forms import Ui_Welcome, Ui_AsistentGui, Ui_Setings_2

# ==================== ФАЙЛИ ТА КОНСТАНТИ ====================
# Спільні шляхи та читання/запис JSON живуть у common.py (їх використовує й асистент).
COMMANDS_FILE = common.commands_file()
USER_FILE = common.user_file()
SETTINGS_FILE = common.settings_file()
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
APP_NAME = "VoiceAssistantGUI"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
API_KEY_URL = "https://aistudio.google.com"


# Відображення логів: значення, що зберігаються в settings.json
LOG_VIS_SHOW = "show"          # показувати всі логи
LOG_VIS_PARTIAL = "partial"    # приховати частково (лише системні повідомлення та помилки)
LOG_VIS_HIDDEN = "hidden"      # приховати повністю
LOG_VIS_LABELS = {
    LOG_VIS_SHOW: "Показувати",
    LOG_VIS_PARTIAL: "Приховати частково",
    LOG_VIS_HIDDEN: "Приховати повністю",
}
LOG_VIS_ORDER = [LOG_VIS_SHOW, LOG_VIS_PARTIAL, LOG_VIS_HIDDEN]   # порядок у випадному списку

DEFAULT_SETTINGS = {
    "assistant_name": "Норі",
    "autostart": False,
    "log_visibility": LOG_VIS_SHOW,
    "volume": 45,
    "tts_model": "",
    "tts_model_id": "",
    "auto_update": True,
    "hide_to_tray": True,
    "mute_microphone": False,
    "auto_start_assistant": False,   # запускати асистента одразу при старті програми
    "log_colors": True,              # розфарбовувати журнал за значенням рядків
}

USER_FIELDS = [
    ("name", "Ім'я"),
    ("country", "Країна"),
    ("region", "Область / Штат"),
    ("city", "Місто / Село"),
    ("time", "Часовий пояс"),
    ("api_key", "API ключ ШІ"),
    ("ai_model", "Модель ШІ"),
]
DEFAULT_USER = {key: "" for key, _ in USER_FIELDS}

# Ідентифікатори моделей у тому ж порядку, що й пункти ModelList у вікні реєстрації.
# Перевірте/виправте назви під ваш API.
MODEL_IDS = ["", "gemini-3.1-flash-lite", "gemini-3-flash"]

IMPORTANT_MARKERS = ("[система]", "помил", "error", "traceback", "exception", "warning", "fail")

# Рядок, який асистент друкує, коли модель завантажена і він справді готовий слухати
READY_MARKER = "Асистент готовий"

# ==================== КОЛЬОРИ ЖУРНАЛУ ====================
# Кожен рядок журналу належить до категорії; категорію визначає тег на початку рядка ([TTS], [AI]...).
# Колір тексту темний, фон — напівпрозора «плашка», тож читається на будь-якій частині синього градієнта.
LOG_STYLES = {
    #  категорія   (колір тексту, фон RGBA,          жирний)
    "error":    ("#8b0000", (255, 205, 205, 220), True),    # помилки
    "warning":  ("#7a4a00", (255, 236, 186, 220), False),   # попередження, «не знайдено», ігнорування
    "success":  ("#0b5d1e", (206, 246, 216, 210), False),   # виконана команда, «асистент готовий»
    "heard":    ("#0a2a7a", (255, 255, 255, 195), False),   # що почув асистент (ваші слова)
    "say":      ("#005b61", (200, 244, 248, 210), False),   # що каже асистент
    "ai":       ("#4a148c", (236, 218, 250, 210), False),   # ШІ, фрази, Whisper
    "activate": ("#8a1250", (255, 220, 236, 210), False),   # активація (слово, F10, F9)
    "system":   ("#26325c", (255, 255, 255, 120), False),   # службові повідомлення
}
LOG_LEGEND = {
    "error": "помилка", "warning": "попередження", "success": "виконано", "heard": "ви сказали",
    "say": "асистент говорить", "ai": "ШІ", "activate": "активація", "system": "службове",
}
_LOG_TIME_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\]\s*")
_LOG_TAG_RE = re.compile(r"^\[([^\]]+)\]\s*(.*)", re.S)
_TAG_CATEGORY = {
    "КРИТИЧНА ПОМИЛКА": "error", "ПОМИЛКА": "error", "НЕ ЗНАЙДЕНО": "warning", "КОМАНДА": "success",
    "РОЗПІЗНАНО": "heard", "ДІАЛОГ": "heard", "TTS": "say",
    "AI": "ai", "ФРАЗИ": "ai", "WHISPER": "ai",
    "АКТИВАЦІЯ": "activate", "HOTKEY": "activate", "F10": "activate",
    "СИСТЕМА": "system", "КОНФІГ": "system", "VOSK": "system", "STATE": "system",
}
_ERROR_WORDS = ("помил", "traceback", "exception", "error", "fail", "не вдалося", "критична")
_WARN_WORDS = ("увага", "warning", "не знайдено", "недоступн", "не відповів", "ігнорую", "втрачено")


def classify_log(line):
    """Категорія рядка журналу (ключ LOG_STYLES). Тег важливіший за слова: «[TTS] >>> Помилка виконання.»
    — це мовлення асистента, а не помилка; ваші розпізнані слова теж не вважаються помилкою."""
    body = _LOG_TIME_RE.sub("", line).strip()
    m = _LOG_TAG_RE.match(body)
    tag, rest = (m.group(1).upper(), m.group(2)) if m else ("", body)
    low = rest.lower()
    if tag == "TTS":
        return "say" if rest.startswith(">>>") else ("error" if "помил" in low else "system")
    if tag == "VOSK":
        return "heard" if rest.startswith("'") else "system"
    category = _TAG_CATEGORY.get(tag)
    if category in ("error", "warning", "success", "heard"):
        return category
    if any(w in low for w in _ERROR_WORDS):
        return "error"
    if any(w in low for w in _WARN_WORDS):
        return "warning"
    if READY_MARKER in body:
        return "success"
    return category or "system"


# ==================== ТЕМА ДІАЛОГІВ (у стилі головного вікна) ====================
_GRAD = ("qlineargradient(spread:reflect, x1:0.638, y1:0.931818, x2:0.282, y2:0.295, "
         "stop:0 rgba(90, 188, 255, 255), stop:1 rgba(40, 66, 174, 255))")
_FIELD = "qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 rgb(176, 228, 255), stop:1 rgb(112, 198, 255))"
_BTN = "qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #59bcff, stop:1 #2842ae)"
_BTN_HOVER = "qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #8fd3ff, stop:1 #3b5bd6)"
_BTN_DOWN = "qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #2842ae, stop:1 #1d2f85)"


_ARROW_PATH = None


def _arrow_image():
    """PNG зі стрілкою для випадних списків (CSS-трикутники Qt малює ненадійно). '' — якщо не вдалося."""
    global _ARROW_PATH
    if _ARROW_PATH is not None and (not _ARROW_PATH or os.path.exists(_ARROW_PATH)):
        return _ARROW_PATH
    try:
        pm = QtGui.QPixmap(12, 8)
        pm.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pm)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor("#0a1640"))
        painter.drawPolygon(QtGui.QPolygon([QtCore.QPoint(1, 1), QtCore.QPoint(11, 1), QtCore.QPoint(6, 7)]))
        painter.end()
        path = os.path.join(tempfile.gettempdir(), "asistent_combo_arrow.png")
        _ARROW_PATH = path if pm.save(path, "PNG") else ""
    except Exception:
        _ARROW_PATH = ""
    return _ARROW_PATH


def dialog_qss(root="QDialog"):
    """Таблиця стилів: той самий градієнт, «плашки» підписів і кнопки, що й у головному вікні."""
    r = root
    arrow = _arrow_image().replace("\\", "/")
    arrow_rule = f'image: url("{arrow}");' if arrow else ""
    return f"""
{r} {{ background-color: {_GRAD}; }}
{r} QLabel {{ background-color: {_GRAD}; color: #000; font-family: "Segoe Print"; font-size: 9pt;
              font-weight: bold; padding: 3px 7px; border-radius: 3px; }}
{r} QLabel#qt_msgboxex_icon_label {{ background: transparent; padding: 0; }}
{r} QLineEdit, {r} QPlainTextEdit, {r} QTextEdit, {r} QComboBox {{
    background-color: {_FIELD}; color: #0a1640; border: 1px solid #1d2a6b; border-radius: 3px;
    padding: 3px 6px; selection-background-color: #2842ae; selection-color: white; }}
{r} QLineEdit:focus, {r} QPlainTextEdit:focus, {r} QTextEdit:focus, {r} QComboBox:focus {{
    border: 2px solid #02bfd4; }}
{r} QComboBox::drop-down {{ border-left: 1px solid #1d2a6b; width: 24px; }}
{r} QComboBox::down-arrow {{ {arrow_rule} width: 12px; height: 8px; margin-right: 6px; }}
{r} QComboBox QAbstractItemView {{ background-color: rgb(176, 228, 255); color: #0a1640;
    border: 1px solid #1d2a6b; selection-background-color: #2842ae; selection-color: white; }}
{r} QPushButton {{ background-color: {_BTN}; color: white; border: 1px solid #1d2a6b; border-radius: 4px;
    padding: 5px 16px; min-width: 84px; font-family: "Segoe UI"; font-size: 10pt; font-weight: bold; }}
{r} QPushButton:hover {{ background-color: {_BTN_HOVER}; }}
{r} QPushButton:pressed {{ background-color: {_BTN_DOWN}; }}
{r} QPushButton:default, {r} QPushButton:focus {{ border: 2px solid #02bfd4; }}
QToolTip {{ background-color: #2842ae; color: white; border: 1px solid #59bcff; padding: 3px; }}
"""


def _resolve_indirect(value):
    """Перетворює рядки виду '@C:\\Windows\\...dll,-101' у справжню назву."""
    if not value.startswith("@"):
        return value
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(512)
        if ctypes.windll.shlwapi.SHLoadIndirectString(value, buf, 512, None) == 0 and buf.value:
            return buf.value
    except Exception:
        pass
    return None


def _read_voice_name(base_key, token):
    """Повертає відображувану назву голосу з розділу-токена реєстру."""
    import winreg
    try:
        with winreg.OpenKey(base_key, token) as k:
            try:
                name = _resolve_indirect(str(winreg.QueryValueEx(k, "")[0]).strip())
                if name:
                    return name
            except OSError:
                pass
            try:
                with winreg.OpenKey(k, "Attributes") as attrs:
                    name = _resolve_indirect(str(winreg.QueryValueEx(attrs, "Name")[0]).strip())
                    if name:
                        return name
            except OSError:
                pass
    except OSError:
        pass
    return token


def _enum_subkeys(hive, path, view):
    import winreg
    try:
        key = winreg.OpenKey(hive, path, 0, winreg.KEY_READ | view)
    except OSError:
        return []
    names = []
    with key:
        i = 0
        while True:
            try:
                names.append(winreg.EnumKey(key, i))
            except OSError:
                break
            i += 1
    return names


def _voice_token_roots(hive, view):
    """
    Усі розділи реєстру виду ...\\Speech*\\[версія\\]Voices\\Tokens.
    Шукаємо динамічно, бо сторонні рушії (Speech Platform, RHVoice, Acapela тощо)
    реєструють голоси в різних місцях.
    """
    roots = []
    for name in _enum_subkeys(hive, r"SOFTWARE\Microsoft", view):
        if name.lower().startswith("speech"):
            base = r"SOFTWARE\Microsoft" + "\\" + name
            roots.append(base + r"\Voices\Tokens")
            for sub in _enum_subkeys(hive, base, view):
                roots.append(base + "\\" + sub + r"\Voices\Tokens")
    return roots


def get_windows_voices():
    """
    Голоси з реєстру Windows: [(назва, id-токена), ...].
    Охоплює SAPI5, OneCore (Narrator), Speech Platform, 32- і 64-бітні розділи.
    """
    if sys.platform != "win32":
        return []
    import winreg

    hives = [(winreg.HKEY_LOCAL_MACHINE, "HKEY_LOCAL_MACHINE"), (winreg.HKEY_CURRENT_USER, "HKEY_CURRENT_USER")]
    views = (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY)

    voices = {}
    for hive, hive_name in hives:
        for view in views:
            for root in _voice_token_roots(hive, view):
                try:
                    base = winreg.OpenKey(hive, root, 0, winreg.KEY_READ | view)
                except OSError:
                    continue
                with base:
                    i = 0
                    while True:
                        try:
                            token = winreg.EnumKey(base, i)
                        except OSError:
                            break
                        i += 1
                        name = _read_voice_name(base, token)
                        if name and name not in voices:
                            voices[name] = hive_name + "\\" + root + "\\" + token
    return sorted(voices.items(), key=lambda kv: kv[0].lower())


_PS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
try {
  Add-Type -AssemblyName System.Speech
  $s = New-Object System.Speech.Synthesis.SpeechSynthesizer
  $s.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name + "`t" + $_.VoiceInfo.Id }
} catch {}
try {
  [void][Windows.Media.SpeechSynthesis.SpeechSynthesizer, Windows.Media.SpeechSynthesis, ContentType=WindowsRuntime]
  [Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices | ForEach-Object { $_.DisplayName + "`t" + $_.Id }
} catch {}
"""


def get_voices_via_powershell():
    """
    Запитує голоси в самої системи (SAPI через System.Speech та WinRT SpeechSynthesizer).
    Знаходить і ті голоси, яких немає в розділах Tokens (наприклад, зареєстровані
    через TokenEnums). Запускає і 64-, і 32-бітний PowerShell.
    """
    if sys.platform != "win32":
        return []
    windir = os.environ.get("SystemRoot", r"C:\Windows")
    exes = []
    for sub in ("System32", "SysWOW64", "Sysnative"):
        path = os.path.join(windir, sub, "WindowsPowerShell", "v1.0", "powershell.exe")
        if os.path.exists(path):
            exes.append(path)
    if not exes:
        exes = ["powershell"]

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    voices = {}
    for exe in exes:
        try:
            result = subprocess.run(
                [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _PS_SCRIPT],
                capture_output=True, timeout=25, creationflags=flags,
            )
        except Exception:
            continue
        for line in result.stdout.decode("utf-8", errors="replace").splitlines():
            name, _, token_id = line.partition("\t")
            name = name.strip().lstrip("\ufeff")
            if name and name not in voices:
                voices[name] = token_id.strip()
    return sorted(voices.items(), key=lambda kv: kv[0].lower())


@contextlib.contextmanager
def blocked(*widgets):
    """Тимчасово вимикає сигнали віджетів (щоб програмне оновлення не викликало збереження)."""
    for w in widgets:
        w.blockSignals(True)
    try:
        yield
    finally:
        for w in widgets:
            w.blockSignals(False)


# ==================== РОБОТА З ФАЙЛАМИ ====================
def load_settings():
    return load_json_file(SETTINGS_FILE, DEFAULT_SETTINGS)


def load_user_data():
    data = dict(DEFAULT_USER)
    data.update(common.load_user())      # API-ключ береться зі сховища Windows, якщо він там є
    return data


def is_registered():
    """Реєстрація пройдена, якщо існує файл user.dat."""
    return os.path.exists(USER_FILE)


def detect_timezone():
    """Повертає рядок на кшталт 'EEST (UTC+3)' — як поле 'time' у user.dat."""
    now = datetime.now().astimezone()
    offset = now.utcoffset()
    minutes = int(offset.total_seconds() // 60) if offset else 0
    sign = "+" if minutes >= 0 else "-"
    h, m = divmod(abs(minutes), 60)
    utc = f"UTC{sign}{h}" + (f":{m:02d}" if m else "")
    return f"{now.tzname()} ({utc})"


# ==================== АВТОЗАПУСК З WINDOWS ====================
def _autostart_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    return f'"{sys.executable}" "{os.path.abspath(__file__)}" --minimized'


def check_autostart():
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, APP_NAME)
        return True
    except OSError:
        return False


def set_autostart(enabled):
    """Повертає (успіх, повідомлення_про_помилку)."""
    if sys.platform != "win32":
        if enabled:
            return False, "Автозавантаження працює лише на Windows."
        return True, ""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_ALL_ACCESS) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _autostart_command())
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        return True, ""
    except Exception as e:
        return False, f"Не вдалося змінити автозавантаження: {e}"


# =====================================================================
#  ВІКНО РЕЄСТРАЦІЇ (колишній welkome.py)
# =====================================================================
# ---------- вікно реєстрації ----------
class WelcomeWindow(QtWidgets.QMainWindow, Ui_Welcome):
    """Вікно привітання: 5 кроків реєстрації + початкові налаштування.

    Після успішного завершення створює user.dat і settings.json та
    випромінює сигнал registrationFinished(user_data, settings).
    """

    registrationFinished = QtCore.pyqtSignal(dict, dict)
    voices_found = QtCore.pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setupUi(self)

        self._finished = False          # True, коли реєстрацію завершено успішно
        self.user_data = load_user_data()
        self.settings = load_settings()

        self.tts_voices = []
        self._scanning_voices = False

        self._prepare_widgets()
        self._connect_signals()
        self._init_tts_voices()          # заповнюємо список голосів ДО підстановки збережених налаштувань
        self._load_into_widgets()
        self._refresh_summary()

    # ----- підготовка інтерфейсу -----
    def _prepare_widgets(self):
        # Текст-підказки з retranslateUi перетворюємо на placeholder,
        # щоб користувачу не доводилось їх стирати
        for edit in (self.lineEdit, self.CountryEdit, self.RegionEdit,
                     self.CityEdit, self.APIKeyEdit):
            edit.setPlaceholderText(edit.text().strip())
            edit.clear()

        # API ключ приховуємо, як у gui.py (show="*")
        self.APIKeyEdit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)

        # QRadioButton за замовчуванням не можна вимкнути повторним кліком
        self.HideToTray.setAutoExclusive(False)

        # Інформаційні списки — лише для читання
        self.LogDisplay.addItems([
            "Ласкаво просимо до Asistent!",
            "Перед початком роботи пройдіть коротку реєстрацію (5 кроків).",
            f"Ваші дані зберігаються локально на цьому комп'ютері: {USER_FILE} та {SETTINGS_FILE}.",
            "Щоб продовжити, поставте галочку «Я Погоджуюся з умовами» і натисніть «>».",
        ])
        self.LocationList.addItems([
            "Країна, область і місто допомагають асистенту враховувати ваше місцезнаходження.",
            "Ці дані необов'язкові — їх можна залишити порожніми.",
        ])
        self._set_hint_model(self.NameList, [
            "Введіть ім'я, як асистент має до вас звертатися.",
            "Його можна змінити пізніше в налаштуваннях.",
        ])
        self._set_hint_model(self.AIList, [
            "Оберіть модель ШІ та вставте свій API ключ.",
            "Ключ можна отримати на сайті aistudio.google.com (кнопка нижче).",
            "Можна залишити «Відсутня» та налаштувати пізніше.",
        ])
        for view in (self.LogDisplay, self.LocationList, self.NameList,
                     self.AIList, self.UsetDataEdit):
            view.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
            view.setWordWrap(True)

    @staticmethod
    def _set_hint_model(view, lines):
        view.setModel(QtCore.QStringListModel(lines, view))

    # ----- голоси Windows (TTS) -----
    def _init_tts_voices(self):
        self.voices_found.connect(self._on_voices_found)
        try:
            self.tts_voices = get_windows_voices()      # швидкий пошук у реєстрі
        except Exception:
            self.tts_voices = []
        self._populate_tts_voices()
        if sys.platform == "win32":                     # повний пошук через систему (у фоні)
            self._scanning_voices = True
            threading.Thread(target=self._scan_voices_thread, daemon=True).start()

    def _scan_voices_thread(self):
        try:
            found = get_voices_via_powershell()
        except Exception:
            found = []
        try:
            self.voices_found.emit(found)
        except RuntimeError:                            # вікно вже закрито
            pass

    def _on_voices_found(self, found):
        self._scanning_voices = False
        known = {name for name, _ in self.tts_voices}
        added = [(n, i) for n, i in found if n not in known]
        if not added:
            return
        current = self.TTSModellList.currentText().strip() if self.TTSModellList.currentIndex() > 0 else ""
        self.tts_voices = sorted(self.tts_voices + added, key=lambda kv: kv[0].lower())
        self._populate_tts_voices()
        self._select_tts(current or str(self.settings.get("tts_model", "")).strip())

    def _populate_tts_voices(self):
        combo = self.TTSModellList
        with blocked(combo):
            while combo.count() > 1:
                combo.removeItem(combo.count() - 1)
            combo.setItemData(0, "")
            for name, token_id in self.tts_voices:
                combo.addItem(name, token_id)
        combo.view().setMinimumWidth(max(combo.width(), 380))

    def _select_tts(self, wanted):
        combo = self.TTSModellList
        index = combo.findText(wanted) if wanted else 0
        with blocked(combo):
            combo.setCurrentIndex(index if index > 0 else 0)
        combo.setToolTip(combo.currentText().strip())

    def _connect_signals(self):
        self.SaveNameButton.clicked.connect(self.save_name_step)
        self.SaveLocationButton.clicked.connect(self.save_location_step)
        self.SaveNameButton_2.clicked.connect(self.save_ai_step)
        self.SaveNameButton_3.clicked.connect(self.finish_registration)
        self.SaiteButton.clicked.connect(self.open_api_site)
        self.stackedWidget.currentChanged.connect(lambda _i: self._refresh_summary())
        self.VolumeSlider.valueChanged.connect(self._on_volume_changed)

    def _load_into_widgets(self):
        """Підставляє збережені user.dat / settings.json у поля."""
        u, s = self.user_data, self.settings

        self.lineEdit.setText(u.get("name", ""))
        self.CountryEdit.setText(u.get("country", ""))
        self.RegionEdit.setText(u.get("region", ""))
        self.CityEdit.setText(u.get("city", ""))
        self.APIKeyEdit.setText(u.get("api_key", ""))
        model = u.get("ai_model", "")
        self.ModelList.setCurrentIndex(MODEL_IDS.index(model) if model in MODEL_IDS else 0)

        self.AsistentNameEdit.setText(str(s.get("assistant_name", DEFAULT_SETTINGS["assistant_name"])))
        try:
            volume = int(s.get("volume", DEFAULT_SETTINGS["volume"]))
        except (TypeError, ValueError):
            volume = DEFAULT_SETTINGS["volume"]
        self.VolumeSlider.setValue(max(0, min(100, volume)))
        self._on_volume_changed(self.VolumeSlider.value())

        autostart = check_autostart() if sys.platform == "win32" else bool(s.get("autostart"))
        self.AutoRunsButton.setChecked(autostart)
        self.AutoUpdateButton.setChecked(bool(s.get("auto_update")))
        self.HideToTray.setChecked(bool(s.get("hide_to_tray")))

        self._select_tts(str(s.get("tts_model", "")).strip())

        saved_filter = s.get("log_filter", [])
        for i in range(self.LogDisplay_2.count()):
            if self._log_filter_for_index(i) == saved_filter:
                self.LogDisplay_2.setCurrentIndex(i)
                break

    # ----- навігація (перевизначає методи з Ui_Welcome) -----
    def _go_to(self, index):
        if index != 0 and not self.checkBox.isChecked():
            QtWidgets.QMessageBox.warning(
                self, "Увага", "Щоб продовжити, потрібно погодитися з умовами.")
            self.checkBox.setFocus()
            return
        self.stackedWidget.setCurrentIndex(index)

    def go_next_page(self):
        cur, total = self.stackedWidget.currentIndex(), self.stackedWidget.count()
        self._go_to(cur + 1 if cur < total - 1 else 0)

    def go_prev_page(self):
        cur, total = self.stackedWidget.currentIndex(), self.stackedWidget.count()
        self._go_to(cur - 1 if cur > 0 else total - 1)

    # ----- збір даних із полів -----
    def _log_filter_for_index(self, index):
        """Пункт 0 ('Всі Логи') -> []; інші -> список категорій."""
        if index <= 0:
            return []
        return [p.strip() for p in self.LogDisplay_2.itemText(index).split(",")]

    def _collect_user_data(self):
        idx = self.ModelList.currentIndex()
        return {
            "name": self.lineEdit.text().strip(),
            "country": self.CountryEdit.text().strip(),
            "region": self.RegionEdit.text().strip(),
            "city": self.CityEdit.text().strip(),
            "time": self.user_data.get("time") or detect_timezone(),
            "api_key": self.APIKeyEdit.text().strip(),
            "ai_model": MODEL_IDS[idx] if 0 <= idx < len(MODEL_IDS) else "",
        }

    def _collect_settings(self):
        settings = dict(self.settings)   # зберігаємо ключі, яких немає в інтерфейсі
        tts_idx = self.TTSModellList.currentIndex()
        settings.update({
            "assistant_name": self.AsistentNameEdit.text().strip(),
            "autostart": self.AutoRunsButton.isChecked(),
            "volume": self.VolumeSlider.value(),
            "tts_model": "" if tts_idx <= 0 else self.TTSModellList.currentText().strip(),
            "tts_model_id": "" if tts_idx <= 0 else (self.TTSModellList.itemData(tts_idx) or ""),
            "auto_update": self.AutoUpdateButton.isChecked(),
            "hide_to_tray": self.HideToTray.isChecked(),
            "log_filter": self._log_filter_for_index(self.LogDisplay_2.currentIndex()),
            # головне вікно читає саме log_visibility: «Всі Логи» -> показувати все, решта -> частково
            "log_visibility": LOG_VIS_SHOW if self.LogDisplay_2.currentIndex() <= 0 else LOG_VIS_PARTIAL,
        })
        return settings

    def _refresh_summary(self):
        user, sett = self._collect_user_data(), self._collect_settings()
        key = user["api_key"]
        yes_no = lambda v: "так" if v else "ні"
        lines = [
            f"Ім'я: {user['name'] or '—'}",
            f"Країна: {user['country'] or '—'}",
            f"Область: {user['region'] or '—'}",
            f"Місто / Село: {user['city'] or '—'}",
            f"Часовий пояс: {user['time']}",
            f"Модель ШІ: {self.ModelList.currentText().strip()}",
            f"API ключ: {'задано (' + str(len(key)) + ' симв.)' if key else 'не вказано'}",
            f"Ім'я асистента: {sett['assistant_name'] or '—'}",
            f"Гучність: {sett['volume']}%",
            f"Авто завантаження: {yes_no(sett['autostart'])}",
            f"Авто оновлення: {yes_no(sett['auto_update'])}",
            f"Приховувати в трей: {yes_no(sett['hide_to_tray'])}",
            f"Логи: {self.LogDisplay_2.currentText()}",
        ]
        self._set_hint_model(self.UsetDataEdit, lines)

    def _on_volume_changed(self, value):
        self.VolumeSlider.setToolTip(f"{value}%")

    # ----- кроки реєстрації -----
    def save_name_step(self):
        name = self.lineEdit.text().strip()
        if not name:
            QtWidgets.QMessageBox.warning(self, "Увага", "Вкажіть ваше ім'я!")
            return
        self.user_data["name"] = name
        self.go_next_page()

    def save_location_step(self):
        self.user_data["country"] = self.CountryEdit.text().strip()
        self.user_data["region"] = self.RegionEdit.text().strip()
        self.user_data["city"] = self.CityEdit.text().strip()
        self.go_next_page()

    def save_ai_step(self):
        if not self._confirm_api_key():
            return
        data = self._collect_user_data()
        self.user_data["api_key"] = data["api_key"]
        self.user_data["ai_model"] = data["ai_model"]
        self.go_next_page()

    def _confirm_api_key(self):
        """Якщо обрано модель, але немає ключа — питаємо, чи продовжувати."""
        if self.ModelList.currentIndex() > 0 and not self.APIKeyEdit.text().strip():
            answer = QtWidgets.QMessageBox.question(
                self, "API ключ",
                "Ви обрали модель ШІ, але не вказали API ключ.\nПродовжити без ключа?",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No)
            return answer == QtWidgets.QMessageBox.StandardButton.Yes
        return True

    def open_api_site(self):
        webbrowser.open(API_KEY_URL)

    # ----- завершення реєстрації -----
    def finish_registration(self):
        if not self.checkBox.isChecked():
            self.stackedWidget.setCurrentIndex(0)
            QtWidgets.QMessageBox.warning(
                self, "Увага", "Щоб завершити реєстрацію, потрібно погодитися з умовами.")
            return

        user = self._collect_user_data()
        if not user["name"]:
            self.stackedWidget.setCurrentIndex(1)
            QtWidgets.QMessageBox.warning(self, "Увага", "Вкажіть ваше ім'я!")
            return

        if not self._confirm_api_key():
            self.stackedWidget.setCurrentIndex(3)
            return

        settings = self._collect_settings()
        if not settings["assistant_name"]:
            QtWidgets.QMessageBox.warning(self, "Увага", "Ім'я асистента не може бути порожнім!")
            self.AsistentNameEdit.setFocus()
            return

        # Автозавантаження застосовуємо до запису, щоб у файлі був реальний стан
        ok, error = set_autostart(settings["autostart"])
        if not ok:
            QtWidgets.QMessageBox.warning(self, "Автозавантаження", error)
            settings["autostart"] = False
            self.AutoRunsButton.setChecked(False)

        try:
            common.save_user(user)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Помилка", f"Не вдалося зберегти {USER_FILE}: {e}")
            return
        try:
            save_json_file(SETTINGS_FILE, settings)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Помилка", f"Не вдалося зберегти {SETTINGS_FILE}: {e}")
            return

        self.user_data, self.settings = user, settings
        QtWidgets.QMessageBox.information(self, "Успіх", f"Ласкаво просимо, {user['name']}!")
        self._finished = True
        self.registrationFinished.emit(user, settings)
        self.close()

    def closeEvent(self, event):
        event.accept()
        if not self._finished:
            # Вікно реєстрації закрито без завершення — виходимо з програми
            # (setQuitOnLastWindowClosed(False) потрібен лише для роботи в треї)
            QtWidgets.QApplication.quit()


# =====================================================================
#  ГОЛОВНЕ ВІКНО ТА НАЛАШТУВАННЯ (колишній gui.py)
# =====================================================================
# ==================== ПЛАВНИЙ ПЕРЕХІД МІЖ ВІКНАМИ ====================
class SlidingStackedWidget(QtWidgets.QStackedWidget):
    """QStackedWidget, який змінює сторінки плавним ковзанням."""

    def __init__(self, parent=None, duration=420):
        super().__init__(parent)
        self._duration = duration
        self._busy = False
        self._group = None

    @property
    def busy(self):
        return self._busy

    def slide_to(self, index):
        current_index = self.currentIndex()
        if self._busy or index == current_index:
            return
        forward = index > current_index
        sign = 1 if forward else -1
        w, h = self.width(), self.height()
        cur = self.widget(current_index)
        nxt = self.widget(index)

        nxt.setGeometry(0, 0, w, h)
        nxt.move(sign * w, 0)
        nxt.show()
        nxt.raise_()

        anim_cur = QtCore.QPropertyAnimation(cur, b"pos")
        anim_cur.setStartValue(QtCore.QPoint(0, 0))
        anim_cur.setEndValue(QtCore.QPoint(-sign * w, 0))
        anim_nxt = QtCore.QPropertyAnimation(nxt, b"pos")
        anim_nxt.setStartValue(QtCore.QPoint(sign * w, 0))
        anim_nxt.setEndValue(QtCore.QPoint(0, 0))
        for anim in (anim_cur, anim_nxt):
            anim.setDuration(self._duration)
            anim.setEasingCurve(QtCore.QEasingCurve.Type.InOutCubic)

        self._group = QtCore.QParallelAnimationGroup(self)
        self._group.addAnimation(anim_cur)
        self._group.addAnimation(anim_nxt)
        self._group.finished.connect(lambda: self._finish(index, cur, nxt))
        self._busy = True
        self._group.start()

    def _finish(self, index, cur, nxt):
        self.setCurrentIndex(index)
        cur.move(0, 0)
        nxt.setGeometry(0, 0, self.width(), self.height())
        self._busy = False


# ==================== ДІАЛОГ СТВОРЕННЯ / РЕДАГУВАННЯ КОМАНДИ ====================
class CommandDialog(QtWidgets.QDialog):
    ACTIONS = ["open_website", "launch_application", "run_command", "hotkey", "hotkey_map",
               "start_dialog", "stop_dialog", "exit"]
    RESERVED = ("keywords", "action", "speak", "disabled")

    def __init__(self, parent, key="", cmd=None):
        super().__init__(parent)
        self.setWindowTitle("Редагування команди" if key else "Нова команда")
        self.setMinimumWidth(600)
        self.resize(640, 440)
        self.setWindowFlag(QtCore.Qt.WindowType.WindowContextHelpButtonHint, False)     # прибираємо кнопку «?»
        if parent is not None and not parent.windowIcon().isNull():
            self.setWindowIcon(parent.windowIcon())
        self.setStyleSheet(dialog_qss("QDialog"))                              # стиль головного вікна
        cmd = cmd or {}

        self.result_key = None
        self.result_cmd = None

        keywords = cmd.get("keywords", [])
        if isinstance(keywords, str):
            keywords = [keywords]
        params = {k: v for k, v in cmd.items() if k not in self.RESERVED}

        self.edit_key = QtWidgets.QLineEdit(key)
        self.edit_key.setPlaceholderText("напр., open_google")
        self.edit_keywords = QtWidgets.QLineEdit(", ".join(keywords))
        self.edit_keywords.setPlaceholderText("інтернет, браузер")
        self.combo_action = QtWidgets.QComboBox()
        self.combo_action.setEditable(True)
        self.combo_action.addItems(self.ACTIONS)
        self.combo_action.setEditText(cmd.get("action", ""))
        self.edit_params = QtWidgets.QPlainTextEdit()
        self.edit_params.setMinimumHeight(120)
        self.edit_params.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont))
        self.edit_params.setPlainText(json.dumps(params, ensure_ascii=False, indent=4) if params else "")
        speak_value = cmd.get("speak", "")
        if isinstance(speak_value, list):
            speak_value = " | ".join(str(v) for v in speak_value)
        self.edit_speak = QtWidgets.QLineEdit(speak_value)
        self.edit_speak.setPlaceholderText("Відкриваю браузер. | Уже відкриваю! | Зараз буде.")
        self.edit_speak.setToolTip("Кілька варіантів розділяйте символом | — асистент щоразу обиратиме випадковий.\n"
                                   "Додаткові варіанти може придумати ШІ (див. кнопку «Фрази: ШІ» у налаштуваннях).")

        for edit in (self.edit_key, self.edit_keywords, self.edit_speak):    # блідіші підказки у полях
            pal = edit.palette()
            pal.setColor(QtGui.QPalette.ColorRole.PlaceholderText, QtGui.QColor(10, 22, 64, 130))
            edit.setPalette(pal)

        form = QtWidgets.QFormLayout(self)
        form.setContentsMargins(16, 16, 16, 14)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.addRow("ID / Ключ команди:", self.edit_key)
        form.addRow("Ключові слова (через кому):", self.edit_keywords)
        form.addRow("Тип дії (action):", self.combo_action)
        form.addRow("Параметри дії (JSON):", self.edit_params)
        form.addRow("Відповідь (варіанти через |):", self.edit_speak)
        self.combo_action.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        for row in range(form.rowCount()):                 # однакова ширина «плашок» підписів
            label = form.itemAt(row, QtWidgets.QFormLayout.ItemRole.LabelRole)
            if label is not None and label.widget() is not None:
                label.widget().setMinimumWidth(250)
        for edit in (self.edit_key, self.edit_keywords, self.edit_speak):
            edit.setCursorPosition(0)                      # довгий текст показуємо з початку

        box = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        box.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText("Зберегти")
        box.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText("Скасувати")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        form.addRow(box)

    def accept(self):
        key = self.edit_key.text().strip()
        if not key:
            QtWidgets.QMessageBox.warning(self, "Увага", "Вкажіть унікальний ключ команди!")
            return

        keywords = [k.strip() for k in self.edit_keywords.text().split(",") if k.strip()]
        action = self.combo_action.currentText().strip()
        parts = [p.strip() for p in self.edit_speak.text().split("|") if p.strip()]
        speak = parts[0] if len(parts) == 1 else parts if parts else ""

        raw = self.edit_params.toPlainText().strip()
        extra = {}
        if raw:
            try:
                extra = json.loads(raw)
            except Exception as e:
                QtWidgets.QMessageBox.critical(self, "Помилка JSON", f"Формат параметрів має бути у JSON:\n{e}")
                return
            if not isinstance(extra, dict):
                QtWidgets.QMessageBox.critical(self, "Помилка JSON", "Параметри мають бути JSON-об'єктом {...}.")
                return

        # Схожі ключові слова: при збігу спрацює команда з довшим словом — краще знати про це заздалегідь
        overlaps = find_overlaps(getattr(self.parent(), "commands_data", {}), key, keywords)
        if overlaps:
            lines = "\n".join(f"• «{mine}» ↔ «{theirs}» (команда {other})" for mine, other, theirs in overlaps[:6])
            box = QtWidgets.QMessageBox(
                QtWidgets.QMessageBox.Icon.Question, "Схожі ключові слова",
                f"Ці слова перетинаються з іншими командами:\n{lines}\n\n"
                "Коли збігаються обидві, спрацює команда з довшим ключовим словом. Зберегти?",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No, self)
            box.button(QtWidgets.QMessageBox.StandardButton.Yes).setText("Так, зберегти")
            box.button(QtWidgets.QMessageBox.StandardButton.No).setText("Ні, змінити")
            box.setDefaultButton(QtWidgets.QMessageBox.StandardButton.Yes)
            if box.exec() != QtWidgets.QMessageBox.StandardButton.Yes:
                return

        self.result_key = key
        self.result_cmd = {"keywords": keywords, "action": action, **extra, "speak": speak}
        super().accept()


# ==================== ОБ'ЄДНАНЕ ГОЛОВНЕ ВІКНО ====================
class AsistentApp(QtWidgets.QMainWindow):
    log_received = QtCore.pyqtSignal(str)
    process_ended = QtCore.pyqtSignal(object)
    voices_found = QtCore.pyqtSignal(list)
    models_checked = QtCore.pyqtSignal(object, str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Asistent 3.0 (тест)")
        self.setFixedSize(800, 420)
        self.app_icon = QtGui.QIcon(resource_path("ico.png"))
        if not self.app_icon.isNull():
            self.setWindowIcon(self.app_icon)

        # Дані
        self.settings = load_settings()
        self.user_data = load_user_data()
        self.commands_data = self.load_commands()

        # Стан асистента
        self.assistant_process = None
        self.assistant_state = None            # idle / active / dialog / thinking / speaking / muted
        self.log_buffer = []
        self.progress_state = "stopped"
        self._force_quit = False
        self.tts_voices = []
        self._scanning_voices = False

        # Дві сторінки в одному вікні: головна (gui) і налаштування (setings)
        self.stack = SlidingStackedWidget(self)
        self.stack.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.stack.setContentsMargins(0, 0, 0, 0)
        self.setCentralWidget(self.stack)

        self.page_main = QtWidgets.QMainWindow(self.stack)
        self.ui_main = Ui_AsistentGui()
        self.ui_main.setupUi(self.page_main)

        self.page_settings = QtWidgets.QMainWindow(self.stack)
        self.ui_set = Ui_Setings_2()
        self.ui_set.setupUi(self.page_settings)

        # QMainWindow завжди має прапорець Qt.Window — робимо з нього звичайний вбудований віджет
        for page in (self.page_main, self.page_settings):
            page.setWindowFlags(QtCore.Qt.WindowType.Widget)

        self.stack.addWidget(self.page_main)
        self.stack.addWidget(self.page_settings)
        self.stack.setCurrentIndex(0)

        self.init_main_page()
        self.init_settings_page()
        self.init_tray()
        self.voices_found.connect(self.on_voices_found)
        try:
            self.tts_voices = get_windows_voices()       # швидкий пошук у реєстрі
        except Exception as e:
            self.append_log(f"[ПОМИЛКА] Не вдалося прочитати голоси Windows: {e}")
        self.populate_tts_voices()
        self.start_voice_scan()                          # повний пошук через систему (у фоні)

        self.log_received.connect(self.on_process_line)
        self.process_ended.connect(self.on_assistant_stopped)

        self.refresh_info()
        self.refresh_log_display()

        for message in LOAD_ERRORS:
            self.append_log(message)
        LOAD_ERRORS.clear()
        if self.settings.get("auto_start_assistant"):
            QtCore.QTimer.singleShot(300, self.start_assistant)

    # ------------------------------------------------------------------
    # ЗАВАНТАЖЕННЯ / ЗБЕРЕЖЕННЯ ДАНИХ
    # ------------------------------------------------------------------
    def load_commands(self):
        if os.path.exists(COMMANDS_FILE):
            try:
                with open(COMMANDS_FILE, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
                raise ValueError("очікувався JSON-об'єкт {...}")
            except Exception as e:
                backup_broken_file(COMMANDS_FILE, e)   # не втратити файл при наступному збереженні
                QtWidgets.QMessageBox.critical(self, "Помилка", f"Не вдалося прочитати {COMMANDS_FILE}: {e}")
        return {}

    def save_commands(self):
        try:
            save_json_file(COMMANDS_FILE, self.commands_data)
            self.notify_assistant("reload")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Помилка", f"Не вдалося зберегти {COMMANDS_FILE}: {e}")

    def save_user_data(self):
        try:
            common.save_user(self.user_data)
            self.notify_assistant("reload")
            return True
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Помилка", f"Не вдалося зберегти {USER_FILE}: {e}")
            return False

    def save_settings(self):
        try:
            save_json_file(SETTINGS_FILE, self.settings)
            self.notify_assistant("reload")
            return True
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Помилка", f"Не вдалося зберегти {SETTINGS_FILE}: {e}")
            return False

    def confirm(self, title, text):
        return QtWidgets.QMessageBox.question(
            self, title, text, QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
        ) == QtWidgets.QMessageBox.StandardButton.Yes

    # ------------------------------------------------------------------
    # ГОЛОВНА СТОРІНКА
    # ------------------------------------------------------------------
    def init_main_page(self):
        m = self.ui_main

        self.info_model = QtCore.QStringListModel(self)
        m.AsistentInfo.setModel(self.info_model)
        m.AsistentInfo.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)

        m.LogDisplay.setSpacing(1)                  # тонкі проміжки між кольоровими рядками
        m.LogDisplay.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        m.LogDisplay.customContextMenuRequested.connect(self.log_context_menu)
        m.LogDisplay.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        m.LogDisplay.setWordWrap(True)

        # Самотній QRadioButton за замовчуванням не можна вимкнути кліком
        m.MuttButton.setAutoExclusive(False)
        m.MuttButton.setChecked(bool(self.settings.get("mute_microphone", False)))
        m.MuttButton.toggled.connect(self.on_mute_toggled)

        m.RunAsistentButton.clicked.connect(self.toggle_assistant)
        m.SetingsButton.clicked.connect(self.open_settings)

        # Індикатор стану (асистент друкує рядки "[STATE] ..."); стоїть у вільному місці зліва
        self.state_label = QtWidgets.QLabel(m.centralwidget)
        self.state_label.setGeometry(QtCore.QRect(10, 125, 171, 90))
        self.state_label.setWordWrap(True)
        self.state_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop | QtCore.Qt.AlignmentFlag.AlignLeft)
        self.state_label.setStyleSheet("background: transparent; color: white; font-size: 13px; font-weight: bold;")
        self.set_progress("stopped")

    def refresh_info(self):
        running = self.assistant_process is not None and self.assistant_process.poll() is None
        lines = [
            f"Ім'я: {self.settings.get('assistant_name', '')}",
            f"Статус: {'працює' if running else 'зупинено'}",
            f"Користувач: {self.user_data.get('name') or '—'}",
            f"Модель ШІ: {self.user_data.get('ai_model') or '—'}",
        ]
        self.info_model.setStringList(lines)

    STATE_TEXT = {
        "idle": "● Очікує слово «{name}» або F10",
        "active": "● Слухаю команду…",
        "dialog": "● Діалог з ШІ.\nСкажіть «стоп діалог», щоб вийти",
        "thinking": "● Думаю…",
        "speaking": "● Говорю (F9 — перервати)",
        "muted": "● Мікрофон вимкнено",
    }

    def update_state_label(self):
        if self.progress_state == "starting":
            text = "● Запуск…"
        elif self.progress_state == "running":
            tpl = self.STATE_TEXT.get(self.assistant_state or "idle", "● Працює")
            text = tpl.format(name=self.settings.get("assistant_name", ""))
        else:
            text = "○ Зупинено"
        self.state_label.setText(text)

    def set_progress(self, state):
        bar = self.ui_main.ProgressBar
        self.progress_state = state
        QtCore.QTimer.singleShot(0, self.update_state_label)
        if state == "starting":
            bar.setRange(0, 0)                      # «крутиться», поки асистент запускається
        elif state == "running":
            bar.setRange(0, 100)
            bar.setValue(100)
            bar.setFormat("Асистент працює")
        else:
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setFormat("Асистент зупинено")

    def on_mute_toggled(self, checked):
        self.settings["mute_microphone"] = bool(checked)
        self.save_settings()
        self.append_log("[СИСТЕМА] Мікрофон вимкнено" if checked else "[СИСТЕМА] Мікрофон увімкнено")

    # ------------------------------------------------------------------
    # ПЕРЕХІД МІЖ ВІКНАМИ
    # ------------------------------------------------------------------
    def open_settings(self):
        if self.stack.busy:
            return
        self.load_settings_into_ui()
        self.refresh_user_list()
        self.refresh_commands_list()
        self.start_voice_scan()
        self.stack.slide_to(1)

    def close_settings(self):
        if self.stack.busy:
            return
        self.save_assistant_name()
        self.refresh_info()
        self.stack.slide_to(0)

    # ------------------------------------------------------------------
    # ЛОГИ
    # ------------------------------------------------------------------
    def _should_display(self, line):
        mode = self.settings.get("log_visibility", LOG_VIS_SHOW)
        if mode == LOG_VIS_HIDDEN:
            return False
        if mode == LOG_VIS_PARTIAL:
            low = line.lower()
            return any(marker in low for marker in IMPORTANT_MARKERS)
        return True

    def _make_log_item(self, line):
        item = QtWidgets.QListWidgetItem(line)
        if self.settings.get("log_colors", True):
            fg, bg, bold = LOG_STYLES[classify_log(line)]
            item.setForeground(QtGui.QBrush(QtGui.QColor(fg)))
            item.setBackground(QtGui.QBrush(QtGui.QColor(*bg)))
            if bold:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
        return item

    def append_log(self, text):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {text}"
        self.log_buffer.append(line)
        if len(self.log_buffer) > 2000:
            self.log_buffer.pop(0)
        if self._should_display(line):
            lw = self.ui_main.LogDisplay
            lw.addItem(self._make_log_item(line))
            while lw.count() > 2000:
                lw.takeItem(0)
            lw.scrollToBottom()

    def refresh_log_display(self):
        lw = self.ui_main.LogDisplay
        lw.clear()
        if self.settings.get("log_visibility", LOG_VIS_SHOW) == LOG_VIS_HIDDEN:
            item = QtWidgets.QListWidgetItem("Логи приховано (Налаштування → Відображення логів)")
            item.setFlags(QtCore.Qt.ItemFlag.NoItemFlags)
            lw.addItem(item)
            return
        for line in self.log_buffer:
            if self._should_display(line):
                lw.addItem(self._make_log_item(line))
        lw.scrollToBottom()

    def _legend_icon(self, category):
        """Квадратик у кольорі категорії (для легенди в меню)."""
        fg, bg, _bold = LOG_STYLES[category]
        pm = QtGui.QPixmap(16, 16)
        pm.fill(QtGui.QColor(fg))
        painter = QtGui.QPainter(pm)
        painter.fillRect(2, 2, 12, 12, QtGui.QColor(*bg[:3]))
        painter.end()
        return QtGui.QIcon(pm)

    def log_context_menu(self, pos):
        menu = QtWidgets.QMenu(self)
        act_copy = menu.addAction("Копіювати")
        act_clear = menu.addAction("Очистити логи")
        menu.addSeparator()
        act_colors = menu.addAction("Кольорові логи")
        act_colors.setCheckable(True)
        act_colors.setChecked(bool(self.settings.get("log_colors", True)))
        legend = menu.addMenu("Що означають кольори")
        for category, label in LOG_LEGEND.items():
            legend.addAction(self._legend_icon(category), label).setEnabled(False)
        chosen = menu.exec(self.ui_main.LogDisplay.mapToGlobal(pos))
        if chosen == act_copy:
            items = self.ui_main.LogDisplay.selectedItems()
            if items:
                QtWidgets.QApplication.clipboard().setText("\n".join(i.text() for i in items))
        elif chosen == act_clear:
            self.log_buffer.clear()
            self.refresh_log_display()
        elif chosen == act_colors:
            self.settings["log_colors"] = act_colors.isChecked()
            try:
                save_json_file(SETTINGS_FILE, self.settings)
            except OSError as e:
                self.append_log(f"[ПОМИЛКА] Не вдалося зберегти {SETTINGS_FILE}: {e}")
            self.refresh_log_display()

    # ------------------------------------------------------------------
    # ЗАПУСК / ЗУПИНКА АСИСТЕНТА
    # ------------------------------------------------------------------
    def find_assistant_command(self):
        """Шукає файл 'asistent' поруч із програмою (як .exe, .py або без розширення)."""
        frozen = getattr(sys, "frozen", False)
        self_exe = os.path.normcase(os.path.abspath(sys.executable)) if frozen else ""
        names = ("asistent3", "asistent", "asistent2v")      # файл може мати номер версії в назві
        candidates = []
        if sys.platform == "win32":
            for name in names:
                candidates.append((os.path.join(BASE_DIR, name + ".exe"), None))
        # У зібраному exe sys.executable — це сама програма, а не Python, тому .py не запускаємо
        if not frozen:
            for name in names:
                candidates.append((os.path.join(BASE_DIR, name + ".py"), sys.executable))
        for name in names:
            candidates.append((os.path.join(BASE_DIR, name), None))
        for path, interpreter in candidates:
            if os.path.isfile(path) and os.path.normcase(os.path.abspath(path)) != self_exe:
                return [interpreter, path] if interpreter else [path]
        return None

    def is_running(self):
        return self.assistant_process is not None and self.assistant_process.poll() is None

    def toggle_assistant(self):
        if self.is_running():
            self.stop_assistant()
        else:
            self.start_assistant()

    def start_assistant(self):
        if self.is_running():
            QtWidgets.QMessageBox.information(self, "Асистент", "Асистент вже запущений.")
            return

        cmd = self.find_assistant_command()
        if not cmd:
            QtWidgets.QMessageBox.critical(
                self, "Помилка",
                "Не знайдено файл 'asistent' (asistent.py / asistent.exe / asistent)\nпоруч із цією програмою.\n\n"
                f"Шукали в теці:\n{BASE_DIR}"
            )
            return

        try:
            env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", ASISTENT_FROM_GUI="1")
            kwargs = dict(
                stdin=subprocess.PIPE,      # через нього GUI каже асистенту "reload" / "quit"
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", bufsize=1, cwd=BASE_DIR, env=env,
            )
            if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            self.assistant_process = subprocess.Popen(cmd, **kwargs)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Помилка запуску", f"Не вдалося запустити асистента:\n{e}")
            return

        self.append_log(f"[СИСТЕМА] Асистента запущено ({' '.join(cmd)})")
        self.ui_main.RunAsistentButton.setText("Вимкнути")
        self.set_progress("starting")
        self.refresh_info()

        threading.Thread(target=self._read_process_output, args=(self.assistant_process,), daemon=True).start()
        # Запасний варіант: якщо рядка «Асистент готовий» немає (інша версія), вважаємо запущеним за 15 с
        QtCore.QTimer.singleShot(15000, self._mark_running_if_alive)

    def _mark_running_if_alive(self):
        if self.progress_state == "starting" and self.is_running():
            self.set_progress("running")

    def _read_process_output(self, proc):
        try:
            for line in iter(proc.stdout.readline, ""):
                if line == "":
                    break
                self.log_received.emit(line.rstrip("\n"))
        except Exception as e:
            self.log_received.emit(f"[ПОМИЛКА ЧИТАННЯ ЛОГІВ] {e}")
        finally:
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            self.process_ended.emit(proc)

    def on_process_line(self, line):
        if line.startswith("[STATE]"):             # службовий рядок: оновлюємо індикатор, у журнал не пишемо
            self.assistant_state = line[7:].strip() or None
            self.update_state_label()
            return
        # «Працює» лише тоді, коли модель завантажена і асистент справді готовий слухати
        if self.progress_state == "starting" and READY_MARKER in line:
            self.set_progress("running")
        self.append_log(line)

    def notify_assistant(self, message):
        """Надсилає асистенту команду ("reload" / "quit") через stdin. True, якщо доставлено."""
        proc = self.assistant_process
        if proc is None or proc.poll() is not None or proc.stdin is None:
            return False
        try:
            proc.stdin.write(message + "\n")
            proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def _terminate(self, proc):
        try:
            proc.terminate()
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Помилка", f"Не вдалося зупинити асистента: {e}")

    def stop_assistant(self, silent=False, wait=False):
        """М'яка зупинка: просимо асистента вийти самому, і лише якщо не вийшов — завершуємо примусово."""
        if self.is_running():
            proc = self.assistant_process
            if not self.notify_assistant("quit"):
                self._terminate(proc)
            elif wait:
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._terminate(proc)
            else:
                QtCore.QTimer.singleShot(3000, lambda p=proc: p.poll() is None and self._terminate(p))
        elif not silent:
            QtWidgets.QMessageBox.information(self, "Асистент", "Асистент не запущений.")

    def on_assistant_stopped(self, proc):
        self.assistant_state = None
        if proc is not self.assistant_process:
            return
        self.append_log("[СИСТЕМА] Асистент завершив роботу.")
        self.ui_main.RunAsistentButton.setText("Увімкнути")
        self.set_progress("stopped")
        self.assistant_process = None
        self.refresh_info()

    # ------------------------------------------------------------------
    # АВТОЗАПУСК З WINDOWS
    # ------------------------------------------------------------------
    def toggle_autostart(self, checked):
        box = self.ui_set.AutoRunsButton
        ok, error = set_autostart(checked)
        if not ok:
            QtWidgets.QMessageBox.warning(self, "Автозапуск", error)
            with blocked(box):
                box.setChecked(not checked)
            return
        self.append_log("[СИСТЕМА] Додано до автозапуску." if checked else "[СИСТЕМА] Видалено з автозапуску.")
        self.settings["autostart"] = bool(checked)
        self.save_settings()

    # ------------------------------------------------------------------
    # СТОРІНКА НАЛАШТУВАНЬ
    # ------------------------------------------------------------------
    def init_settings_page(self):
        s = self.ui_set

        # --- Загальні ---
        s.TTSModellList.currentIndexChanged.connect(self.on_tts_changed)
        s.VolumeSlider.valueChanged.connect(self.on_volume_changed)
        s.VolumeSlider.sliderReleased.connect(self.save_settings)
        s.AsistentNameEdit.editingFinished.connect(self.save_assistant_name)
        s.LogDisplay.currentIndexChanged.connect(self.on_log_visibility_change)
        s.AutoRunsButton.toggled.connect(self.toggle_autostart)
        s.AutoUpdateButton.toggled.connect(lambda v: self.set_flag("auto_update", v))
        s.HideToTray.setAutoExclusive(False)    # інакше самотню радіокнопку не можна вимкнути
        s.HideToTray.toggled.connect(lambda v: self.set_flag("hide_to_tray", v))
        s.BackButton.clicked.connect(self.close_settings)
        s.AutoUpdateButton.setToolTip("Поки що не реалізовано — параметр зарезервований")

        # Кнопка під списком даних користувача: перевірити ключ і обрати модель зі списку API
        s.UsetDataEdit.setGeometry(QtCore.QRect(425, 60, 341, 195))
        bold = QtGui.QFont("Segoe UI", 10, QtGui.QFont.Weight.Bold)
        self.btn_check_key = QtWidgets.QPushButton("Перевірити ключ і обрати модель", s.tab_1)
        self.btn_check_key.setGeometry(QtCore.QRect(425, 262, 341, 34))
        self.btn_check_key.setFont(bold)
        self.btn_check_key.clicked.connect(self.check_api_key)

        # Різноманітні відповіді: власний файл варіантів і генерація додаткових варіантів через ШІ
        self.btn_phrases_file = QtWidgets.QPushButton("Фрази: файл", s.tab_1)
        self.btn_phrases_file.setGeometry(QtCore.QRect(425, 302, 166, 34))
        self.btn_phrases_file.setFont(bold)
        self.btn_phrases_file.setToolTip("Відкрити phrases.json — власні варіанти відповідей "
                                         "(якщо файлу немає, створиться із заготовкою)")
        self.btn_phrases_file.clicked.connect(self.open_phrases_file)
        self.btn_phrases_ai = QtWidgets.QPushButton("Фрази: ШІ", s.tab_1)
        self.btn_phrases_ai.setGeometry(QtCore.QRect(600, 302, 166, 34))
        self.btn_phrases_ai.setFont(bold)
        self.btn_phrases_ai.setToolTip("Попросити ШІ придумати нові варіанти відповідей (асистент має працювати)")
        self.btn_phrases_ai.clicked.connect(self.generate_phrases_ai)
        self.models_checked.connect(self.on_models_checked)

        self.user_model = QtGui.QStandardItemModel(self)
        s.UsetDataEdit.setModel(self.user_model)
        s.UsetDataEdit.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        s.UsetDataEdit.setToolTip("Двічі клікніть по рядку, щоб змінити значення")
        s.UsetDataEdit.doubleClicked.connect(self.edit_user_field)

        # --- Команди ---
        self.cmd_model = QtGui.QStandardItemModel(self)
        s.ComandList.setModel(self.cmd_model)
        s.ComandList.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        s.ComandList.selectionModel().selectionChanged.connect(self.update_preview)

        self.preview_model = QtCore.QStringListModel(self)
        s.ComandPreivew.setModel(self.preview_model)
        s.ComandPreivew.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)

        # Перевірка фрази: яка команда спрацює (з урахуванням вимкнених і найдовшого збігу)
        s.ComandPreivew.setGeometry(QtCore.QRect(170, 10, 601, 215))
        self.test_edit = QtWidgets.QLineEdit(s.tab_2)
        self.test_edit.setGeometry(QtCore.QRect(170, 232, 330, 34))
        self.test_edit.setPlaceholderText("Фраза для перевірки, напр.: відкрий браузер")
        self.test_btn = QtWidgets.QPushButton("Що спрацює?", s.tab_2)
        self.test_btn.setGeometry(QtCore.QRect(510, 232, 261, 34))
        self.test_btn.setFont(QtGui.QFont("Segoe UI", 10, QtGui.QFont.Weight.Bold))
        self.test_btn.clicked.connect(self.test_command_phrase)
        self.test_edit.returnPressed.connect(self.test_command_phrase)

        s.CommandDelitButton.clicked.connect(self.delete_command)
        s.CommandOFFButton.clicked.connect(self.toggle_command)
        s.CommandEditButton.clicked.connect(self.edit_command)
        s.CommandCreateButton.clicked.connect(self.create_command)
        s.ImportScriptButton.clicked.connect(self.import_script)
        s.OpenScriptsFolder.clicked.connect(self.open_scripts_folder)

    def load_settings_into_ui(self):
        s = self.ui_set
        st = self.settings
        with blocked(s.TTSModellList, s.VolumeSlider, s.AsistentNameEdit, s.LogDisplay,
                     s.AutoRunsButton, s.AutoUpdateButton, s.HideToTray):
            s.AsistentNameEdit.setText(st.get("assistant_name", DEFAULT_SETTINGS["assistant_name"]))
            s.VolumeSlider.setValue(int(st.get("volume", 45)))
            s.VolumeSlider.setToolTip(f"{s.VolumeSlider.value()}%")

            self.populate_tts_voices()
            self.apply_tts_selection()

            mode = st.get("log_visibility", LOG_VIS_SHOW)
            s.LogDisplay.setCurrentIndex(LOG_VIS_ORDER.index(mode) if mode in LOG_VIS_ORDER else 0)

            actual_autostart = check_autostart()
            s.AutoRunsButton.setChecked(actual_autostart)
            self.settings["autostart"] = actual_autostart
            s.AutoUpdateButton.setChecked(bool(st.get("auto_update", True)))
            s.HideToTray.setChecked(bool(st.get("hide_to_tray", True)))

    def set_flag(self, key, value):
        self.settings[key] = bool(value)
        self.save_settings()

    def populate_tts_voices(self):
        """Заповнює список голосами, знайденими у Windows."""
        combo = self.ui_set.TTSModellList
        with blocked(combo):
            while combo.count() > 1:
                combo.removeItem(combo.count() - 1)
            combo.setItemData(0, "")                      # перший пункт — «Відсутня»
            for name, token_id in self.tts_voices:
                combo.addItem(name, token_id)
        combo.view().setMinimumWidth(max(combo.width(), 380))   # щоб довгі назви влазили

    def apply_tts_selection(self):
        """Виділяє у списку збережений голос (за id або назвою)."""
        combo = self.ui_set.TTSModellList
        wanted = (self.settings.get("tts_model") or "").strip()
        wanted_id = self.settings.get("tts_model_id") or ""
        with blocked(combo):
            index = 0
            if wanted or wanted_id:
                index = -1
                for i in range(1, combo.count()):
                    if (wanted_id and combo.itemData(i) == wanted_id) or combo.itemText(i) == wanted:
                        index = i
                        break
                if index < 0:       # збережений голос зараз не знайдено — показуємо його, щоб не втратити вибір
                    combo.addItem(wanted or wanted_id, wanted_id)
                    index = combo.count() - 1
            combo.setCurrentIndex(index)
            combo.setToolTip(combo.currentText().strip())

    def start_voice_scan(self):
        if sys.platform != "win32" or self._scanning_voices:
            return
        self._scanning_voices = True
        threading.Thread(target=self._scan_voices_thread, daemon=True).start()

    def _scan_voices_thread(self):
        try:
            found = get_voices_via_powershell()
        except Exception:
            found = []
        self.voices_found.emit(found)

    def on_voices_found(self, found):
        self._scanning_voices = False
        known = {name for name, _ in self.tts_voices}
        added = [(n, i) for n, i in found if n not in known]
        if not added:
            return
        self.tts_voices = sorted(self.tts_voices + added, key=lambda kv: kv[0].lower())
        self.populate_tts_voices()
        self.apply_tts_selection()
        self.append_log(f"[СИСТЕМА] Знайдено додаткових голосів Windows: {len(added)} "
                        f"({', '.join(n for n, _ in added)})")

    def on_tts_changed(self, index):
        combo = self.ui_set.TTSModellList
        if index <= 0:
            self.settings["tts_model"] = ""
            self.settings["tts_model_id"] = ""
        else:
            self.settings["tts_model"] = combo.itemText(index).strip()
            self.settings["tts_model_id"] = combo.itemData(index) or ""
        combo.setToolTip(combo.currentText().strip())
        self.save_settings()

    def on_volume_changed(self, value):
        self.settings["volume"] = int(value)
        self.ui_set.VolumeSlider.setToolTip(f"{value}%")
        if not self.ui_set.VolumeSlider.isSliderDown():
            self.save_settings()

    def save_assistant_name(self):
        edit = self.ui_set.AsistentNameEdit
        name = edit.text().strip()
        if not name:
            QtWidgets.QMessageBox.warning(self, "Увага", "Ім'я асистента не може бути порожнім!")
            with blocked(edit):
                edit.setText(self.settings.get("assistant_name", DEFAULT_SETTINGS["assistant_name"]))
            return
        if name == self.settings.get("assistant_name"):
            return
        self.settings["assistant_name"] = name
        if self.save_settings():
            self.append_log(f"[СИСТЕМА] Ім'я асистента змінено на «{name}» та збережено у {SETTINGS_FILE}.")
        self.refresh_info()

    def on_log_visibility_change(self, index):
        if 0 <= index < len(LOG_VIS_ORDER):
            self.settings["log_visibility"] = LOG_VIS_ORDER[index]
            self.save_settings()
            self.refresh_log_display()

    # ---- Дані користувача ----
    def refresh_user_list(self):
        self.user_model.clear()
        for key, label in USER_FIELDS:
            value = self.user_data.get(key, "") or ""
            shown = ("•" * 8 if value else "") if key == "api_key" else value
            item = QtGui.QStandardItem(f"{label}: {shown}")
            item.setData(key, QtCore.Qt.ItemDataRole.UserRole)
            item.setEditable(False)
            self.user_model.appendRow(item)

    def edit_user_field(self, index):
        key = index.data(QtCore.Qt.ItemDataRole.UserRole)
        label = dict(USER_FIELDS).get(key, key)
        mode = QtWidgets.QLineEdit.EchoMode.Password if key == "api_key" else QtWidgets.QLineEdit.EchoMode.Normal
        text, ok = QtWidgets.QInputDialog.getText(
            self, "Редагування даних користувача", f"{label}:", mode, self.user_data.get(key, "") or ""
        )
        if not ok:
            return
        text = text.strip()
        if key == "name" and not text:
            QtWidgets.QMessageBox.warning(self, "Увага", "Вкажіть ваше ім'я!")
            return
        self.user_data[key] = text
        if self.save_user_data():
            self.append_log(f"[СИСТЕМА] Дані користувача оновлено: {label}.")
        self.refresh_user_list()
        self.refresh_info()

    def check_api_key(self):
        key = (self.user_data.get("api_key") or "").strip()
        if not key:
            QtWidgets.QMessageBox.warning(self, "API ключ", "Спершу вкажіть API ключ (двічі клікніть по рядку «API ключ ШІ»).")
            return
        self.btn_check_key.setEnabled(False)
        self.btn_check_key.setText("Перевіряю…")
        threading.Thread(target=self._check_key_thread, args=(key,), daemon=True).start()

    def _check_key_thread(self, key):
        try:
            self.models_checked.emit(ai.list_models(key), "")
        except ai.AIError as e:
            self.models_checked.emit(None, e.user_message)
        except Exception as e:
            self.models_checked.emit(None, str(e))

    def on_models_checked(self, models, error):
        self.btn_check_key.setEnabled(True)
        self.btn_check_key.setText("Перевірити ключ і обрати модель")
        if error or not models:
            QtWidgets.QMessageBox.warning(self, "Ключ не підійшов", error or "Для цього ключа немає доступних моделей.")
            return
        current = self.user_data.get("ai_model") or ""
        index = models.index(current) if current in models else 0
        item, ok = QtWidgets.QInputDialog.getItem(
            self, "Ключ працює", f"Знайдено моделей: {len(models)}. Оберіть модель для діалогу:",
            models, index, False)
        if ok and item:
            self.user_data["ai_model"] = item
            if self.save_user_data():
                self.append_log(f"[СИСТЕМА] Модель ШІ: {item}")
            self.refresh_user_list()
            self.refresh_info()

    def open_phrases_file(self):
        path = common.path_of("phrases.json")
        if not os.path.exists(path):
            try:
                phrases_mod.write_default(path)
            except OSError as e:
                QtWidgets.QMessageBox.critical(self, "Помилка", f"Не вдалося створити {path}: {e}")
                return
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))
        self.append_log("[СИСТЕМА] Відредагуйте phrases.json і збережіть — асистент підхопить зміни сам.")

    def generate_phrases_ai(self):
        if not (self.user_data.get("api_key") or "").strip():
            QtWidgets.QMessageBox.warning(self, "Фрази", "Для генерації потрібен API ключ ШІ (вкладка «Загальні»).")
            return
        if not self.is_running():
            QtWidgets.QMessageBox.information(
                self, "Фрази", "Спершу запустіть асистента: варіанти генерує він. Це відбудеться й автоматично "
                               "через кілька секунд після старту.")
            return
        self.notify_assistant("phrases")
        self.append_log("[СИСТЕМА] Запит на генерацію варіантів фраз надіслано — це може тривати до хвилини.")

    def test_command_phrase(self):
        text = self.test_edit.text().strip().lower()
        if not text:
            return
        matches = match_commands(self.commands_data, text)
        if matches:
            order = "\n".join(f"{i}. {key} ({info.get('action', '?')})" for i, (key, info) in enumerate(matches, 1))
            message = f"Спрацює команда «{matches[0][0]}».\n\nВсі збіги за пріоритетом:\n{order}"
        else:
            message = ("Жодна з ваших команд не підходить.\n\nМожливо, спрацює вбудована (час, дата, таймер, "
                       "гучність, нотатки, пошук) або фразу обробить ШІ.")
        QtWidgets.QMessageBox.information(self, "Перевірка фрази", message)

    # ---- Команди ----
    def current_command_key(self):
        idx = self.ui_set.ComandList.currentIndex()
        return idx.data(QtCore.Qt.ItemDataRole.UserRole) if idx.isValid() else None

    def refresh_commands_list(self, select_key=None):
        if select_key is None:
            select_key = self.current_command_key()
        self.cmd_model.clear()
        select_row = None
        for row, key in enumerate(sorted(self.commands_data.keys())):
            cmd = self.commands_data.get(key) or {}
            disabled = isinstance(cmd, dict) and cmd.get("disabled")
            item = QtGui.QStandardItem(f"{key}  (вимкнено)" if disabled else key)
            item.setData(key, QtCore.Qt.ItemDataRole.UserRole)
            item.setEditable(False)
            if disabled:
                item.setForeground(QtGui.QBrush(QtGui.QColor("#555555")))
            self.cmd_model.appendRow(item)
            if key == select_key:
                select_row = row
        if select_row is not None:
            self.ui_set.ComandList.setCurrentIndex(self.cmd_model.index(select_row, 0))
        self.update_preview()

    @staticmethod
    def _fmt_value(value):
        if isinstance(value, (list, tuple)):
            return ", ".join(str(v) for v in value)
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    def update_preview(self, *_):
        s = self.ui_set
        key = self.current_command_key()
        if key is None or key not in self.commands_data:
            self.preview_model.setStringList([])
            s.CommandOFFButton.setText("Вимкнути")
            return

        cmd = self.commands_data[key] or {}
        disabled = bool(cmd.get("disabled"))
        s.CommandOFFButton.setText("Увімкнути" if disabled else "Вимкнути")

        names = {"keywords": "Ключові слова", "action": "Дія", "speak": "Відповідь"}
        lines = [f"Ключ: {key}", f"Стан: {'вимкнено' if disabled else 'увімкнено'}"]
        for k, v in cmd.items():
            if k == "disabled":
                continue
            lines.append(f"{names.get(k, k)}: {self._fmt_value(v)}")
        self.preview_model.setStringList(lines)

    def _need_selection(self):
        key = self.current_command_key()
        if key is None:
            QtWidgets.QMessageBox.information(self, "Увага", "Спочатку оберіть команду зі списку.")
        return key

    def create_command(self):
        dlg = CommandDialog(self)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        key, cmd = dlg.result_key, dlg.result_cmd
        if key in self.commands_data and not self.confirm("Підтвердження", f"Команда '{key}' вже існує. Перезаписати?"):
            return
        self.commands_data[key] = cmd
        self.save_commands()
        self.refresh_commands_list(select_key=key)
        self.append_log(f"[СИСТЕМА] Команду '{key}' збережено.")

    def edit_command(self):
        old_key = self._need_selection()
        if old_key is None:
            return
        old_cmd = self.commands_data.get(old_key, {})
        dlg = CommandDialog(self, old_key, old_cmd)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        key, cmd = dlg.result_key, dlg.result_cmd
        if key != old_key and key in self.commands_data:
            if not self.confirm("Підтвердження", f"Команда '{key}' вже існує. Перезаписати?"):
                return
        if old_cmd.get("disabled"):
            cmd["disabled"] = True
        if key != old_key:
            self.commands_data.pop(old_key, None)
        self.commands_data[key] = cmd
        self.save_commands()
        self.refresh_commands_list(select_key=key)
        self.append_log(f"[СИСТЕМА] Команду '{key}' оновлено.")

    def delete_command(self):
        key = self._need_selection()
        if key is None:
            return
        if self.confirm("Підтвердження", f"Видалити команду '{key}'?"):
            self.commands_data.pop(key, None)
            self.save_commands()
            self.refresh_commands_list(select_key="")
            self.append_log(f"[СИСТЕМА] Команду '{key}' видалено.")

    def toggle_command(self):
        key = self._need_selection()
        if key is None:
            return
        cmd = self.commands_data.get(key, {})
        if cmd.get("disabled"):
            cmd.pop("disabled", None)
            self.append_log(f"[СИСТЕМА] Команду '{key}' увімкнено.")
        else:
            cmd["disabled"] = True
            self.append_log(f"[СИСТЕМА] Команду '{key}' вимкнено.")
        self.commands_data[key] = cmd
        self.save_commands()
        self.refresh_commands_list(select_key=key)

    def import_script(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Імпорт скрипта", "", "Скрипти (*.py *.bat *.cmd *.ps1 *.sh *.js);;Усі файли (*)"
        )
        if not path:
            return
        try:
            os.makedirs(SCRIPTS_DIR, exist_ok=True)
            target = os.path.join(SCRIPTS_DIR, os.path.basename(path))
            if os.path.exists(target) and not self.confirm(
                    "Підтвердження", f"Файл '{os.path.basename(path)}' вже є в теці скриптів. Замінити?"):
                return
            shutil.copy2(path, target)
            self.append_log(f"[СИСТЕМА] Скрипт імпортовано: {os.path.basename(path)}")
            QtWidgets.QMessageBox.information(self, "Успіх", f"Скрипт скопійовано до:\n{SCRIPTS_DIR}")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Помилка", f"Не вдалося імпортувати скрипт: {e}")

    def open_scripts_folder(self):
        try:
            os.makedirs(SCRIPTS_DIR, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(SCRIPTS_DIR)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", SCRIPTS_DIR])
            else:
                subprocess.Popen(["xdg-open", SCRIPTS_DIR])
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Помилка", f"Не вдалося відкрити теку: {e}")

    # ------------------------------------------------------------------
    # ТРЕЙ ТА ЗАКРИТТЯ ПРОГРАМИ
    # ------------------------------------------------------------------
    def init_tray(self):
        self.tray = None
        if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            return
        icon = self.app_icon if not self.app_icon.isNull() else \
            self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_ComputerIcon)
        self.tray = QtWidgets.QSystemTrayIcon(icon, self)
        self.tray.setToolTip("Asistent")
        menu = QtWidgets.QMenu()
        menu.addAction("Показати", self.restore_from_tray)
        auto_run = menu.addAction("Запускати асистента при старті")
        auto_run.setCheckable(True)
        auto_run.setChecked(bool(self.settings.get("auto_start_assistant", False)))
        auto_run.toggled.connect(lambda v: self.set_flag("auto_start_assistant", v))
        menu.addAction("Вийти", self.quit_app)
        self.tray.setContextMenu(menu)
        self._tray_menu = menu
        self.tray.activated.connect(
            lambda reason: self.restore_from_tray()
            if reason in (QtWidgets.QSystemTrayIcon.ActivationReason.Trigger, QtWidgets.QSystemTrayIcon.ActivationReason.DoubleClick) else None
        )

    def restore_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()
        if self.tray:
            self.tray.hide()

    def confirm_exit(self):
        if self.is_running():
            return self.confirm("Вихід", "Асистент усе ще працює. Зупинити його та вийти?")
        return True

    def quit_app(self):
        if self.confirm_exit():
            self._force_quit = True
            self.close()

    def closeEvent(self, event):
        if not self._force_quit:
            if self.settings.get("hide_to_tray", True) and self.tray is not None:
                event.ignore()
                self.hide()
                self.tray.show()
                self.tray.showMessage("Asistent", "Програма працює у треї.",
                                      QtWidgets.QSystemTrayIcon.MessageIcon.Information, 2000)
                return
            if not self.confirm_exit():
                event.ignore()
                return
        self.stop_assistant(silent=True, wait=True)
        if self.tray:
            self.tray.hide()
        event.accept()
        QtWidgets.QApplication.quit()


# ==================== ТОЧКА ВХОДУ ====================
def main():
    # Qt6: HiDPI-масштабування ввімкнене завжди; лише просимо точні (не округлені) коефіцієнти
    QtGui.QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        QtCore.Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)    # потрібно для роботи в треї

    # Лише один екземпляр: інакше два асистенти боротимуться за мікрофон і клавішу F10
    guard = QtCore.QSharedMemory(APP_NAME + "_single_instance")
    if guard.attach():                      # Linux: сегмент міг лишитися після аварійного завершення
        guard.detach()
    if not guard.create(1):
        QtWidgets.QMessageBox.information(None, "Asistent", "Програма вже запущена (перевірте трей).")
        return

    # Оновлюємо запис автозапуску (шлях, прапорець --minimized), якщо він увімкнений
    if check_autostart():
        set_autostart(True)

    minimized = "--minimized" in sys.argv[1:]
    windows = {"guard": guard}              # тримаємо посилання, щоб вікна/сегмент не знищив збирач сміття

    def open_main_window():
        window = AsistentApp()
        windows["main"] = window
        if minimized and window.tray is not None:
            window.tray.show()              # автозапуск: стартуємо згорнутими в трей
        else:
            window.show()

    if is_registered():
        open_main_window()
    else:
        welcome = WelcomeWindow()
        welcome.registrationFinished.connect(lambda _user, _settings: open_main_window())
        windows["welcome"] = welcome
        welcome.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()