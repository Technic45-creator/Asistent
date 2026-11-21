# Асистент 3.0 (тестова версія)
import sys
import os

# ─── stdout/stderr: у збірці без консолі вони None і print() падає ────────────
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = sys.stdout
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", line_buffering=True)   # у pipe (GUI) без цього логи йдуть із затримкою
    except Exception:
        pass

import json
import queue
import threading
import time

import common
from common import ASSISTANT_DEFAULTS, setup_logging

log = setup_logging("asistent")


def msgbox(title, text, icon=0x10):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, icon)
    except Exception:
        pass


def fatal(text):
    log.info(f"[КРИТИЧНА ПОМИЛКА] {text}")
    msgbox("Асистент — помилка", text, 0x10)
    os._exit(1)


try:
    import sounddevice as sd
    import keyboard
    from vosk import Model, KaldiRecognizer, SetLogLevel
    from ai import Gemini, AIError, DEFAULT_MODEL
    from commands import Commands
    from matching import has_kw
    from phrases import Phrases
    from stt import WhisperSTT
    from tts import TTS
except Exception as e:      # noqa: BLE001
    fatal(f"Не вдалося завантажити необхідну бібліотеку:\n{e}")

SAMPLE_RATE = 16000
BLOCK_SIZE = 4000                 # 0.25 с аудіо
EXTRA_WAKE_WORDS = ("асистент",)
WAKE_ALIASES = {"норі": ("нові",)}      # типові помилки розпізнавання для імені за замовчуванням
DIALOG_EXIT_WORDS = ("стоп діалог", "закінчи діалог", "вийди з діалогу",
                     "завершити діалог", "досить говорити", "припини діалог")
MAX_UTTERANCE_BYTES = SAMPLE_RATE * 2 * 30      # не більше 30 с звуку для Whisper


class Assistant:
    def __init__(self):
        self.commands_data, self.settings, self.user = {}, {}, {}
        self.name = ASSISTANT_DEFAULTS["assistant_name"]
        self.user_name = "друже"
        self.api_key = ""
        self.model_id = DEFAULT_MODEL
        self.wake_words = ()
        self.muted = False

        self.is_active = False
        self._active_since = 0.0
        self.dialog_mode = False
        self.ai_busy = threading.Event()        # йде запит до AI — мікрофон тимчасово ігноруємо
        self._state = None
        self._phrases_mtime = None         # None = ще не дивились
        self._phrases_checked = 0.0

        self.tts = TTS(self.cfg)
        # Різноманітні відповіді: phrases.json + вбудовані + варіанти, згенеровані ШІ (phrases_ai.json)
        self.P = Phrases(common.path_of("phrases.json"), common.path_of("phrases_ai.json"))
        self.commands = Commands(self.tts.speak, self.cfg, self.P,
                                 lambda: common.path_of(str(self.cfg("notes_file", "notes.txt"))), keyboard)
        self.ai = Gemini(self.cfg, lambda: self.api_key, lambda: self.model_id,
                         self.system_prompt, self._tool_decls, self.commands.call_tool)
        self.whisper = None
        self.audio_q = queue.Queue()

    # ── налаштування ─────────────────────────────────────────────────────────
    def cfg(self, key, default=None):
        value = self.settings.get(key)
        if value is None:
            value = ASSISTANT_DEFAULTS.get(key, default)
        return default if value is None and default is not None else value

    def load_config(self, first_run=False):
        """Читає comand.json, user.dat, seting.json. Якщо файл тимчасово нечитабельний —
        лишає старі значення."""
        commands = common.read_json(common.commands_file())
        settings = common.read_json(common.settings_file())
        user = common.read_json(common.user_file())
        if commands is None:
            commands = {} if first_run else self.commands_data
            if first_run:
                msgbox("Асистент — увага",
                       f"Файл comand.json не знайдено або він пошкоджений.\nПокладіть його поруч з програмою:\n"
                       f"{common.BASE_DIR}", 0x30)
        if settings is None:
            settings = {} if first_run else self.settings
        if user is None:
            user = {} if first_run else self.user

        self.commands_data, self.settings, self.user = commands, settings, user
        self.commands.set_commands(commands)
        self.user_name = user.get("name") or "друже"
        self.api_key = common.get_api_key(user)
        self.model_id = user.get("ai_model") or DEFAULT_MODEL
        self.name = str(settings.get("assistant_name") or "").strip() or ASSISTANT_DEFAULTS["assistant_name"]
        self.muted = bool(self.cfg("mute_microphone", False))
        n = self.name.lower()
        extra = [str(w).lower() for w in (self.cfg("wake_words_extra", []) or [])]
        self.wake_words = tuple(dict.fromkeys([n, *EXTRA_WAKE_WORDS, *WAKE_ALIASES.get(n, ()), *extra]))
        self.P.configure(name=self.name, user=self.user_name, style=str(self.cfg("phrases_style", "") or ""),
                         use_ai=bool(self.cfg("phrases_ai", True)))
        self.P.reload()
        self.P.set_commands(commands)
        log.info(f"[КОНФІГ] Ім'я: {self.name}; команд: {len(commands)}; "
                 f"мікрофон: {'вимкнено' if self.muted else 'увімкнено'}; "
                 f"озвучення: {self.cfg('tts_backend')}; діалог: {self.cfg('dialog_stt')}")

    def check_phrase_file(self):
        """Раз на секунду дивиться, чи змінили phrases.json, і підхоплює зміни без перезапуску."""
        now = time.monotonic()
        if now - self._phrases_checked < 1.0:
            return
        self._phrases_checked = now
        try:
            mtime = os.path.getmtime(self.P.file_path)
        except OSError:
            mtime = 0.0
        if mtime != self._phrases_mtime:
            first = self._phrases_mtime is None
            self._phrases_mtime = mtime
            if not first:
                self.P.reload()
                log.info("[ФРАЗИ] phrases.json оновлено.")
                self.generate_phrases()      # еталони могли змінитися — доберемо варіанти ШІ

    def reload_config(self):
        log.info("[СИСТЕМА] Перечитую налаштування...")
        self.load_config()
        self.generate_phrases()            # лише для нових/змінених фраз

    def generate_phrases(self, force=False):
        """Фоном просить ШІ дописати варіанти фраз і кешує їх у phrases_ai.json."""
        if not (self.api_key and self.cfg("phrases_ai", True)):
            if force:
                log.info("[ФРАЗИ] Генерація вимкнена (phrases_ai) або не вказано API ключ.")
            return

        def work():
            try:
                n = self.P.generate(self.api_key, self.model_id, force=force,
                                    per_key=int(self.cfg("phrases_ai_variants", 6)),
                                    refresh_days=int(self.cfg("phrases_ai_refresh_days", 7)))
                if force and not n:
                    log.info("[ФРАЗИ] Нових варіантів не додано (генерація вже йде або ШІ не відповів).")
            except Exception as e:
                log.info(f"[ФРАЗИ] Помилка генерації: {e}")

        threading.Thread(target=work, daemon=True, name="phrases-ai").start()

    # ── AI ───────────────────────────────────────────────────────────────────
    def system_prompt(self):
        place = ", ".join(p for p in (self.user.get("city"), self.user.get("region"),
                                      self.user.get("country")) if p)
        extra = (f" Користувач перебуває: {place}." if place else "") + \
                (f" Його часовий пояс: {self.user['time']}." if self.user.get("time") else "")
        return (f"Ти — голосовий асистент на ім'я {self.name}. Ти розмовляєш з користувачем на ім'я "
                f"{self.user_name}.{extra} Відповідай українською мовою, коротко (1-3 речення), природно, "
                "у живому розмовному стилі, адже відповіді будуть озвучені голосом. "
                "Якщо користувач просить щось зробити на комп'ютері (відкрити сайт чи програму, таймер, "
                "гучність, нотатка, пошук) — викликай відповідну функцію, а не відповідай, що не можеш.")

    def _tool_decls(self):
        return self.commands.tool_declarations() if self.cfg("ai_tools", True) else None

    def _start_ai(self, text, keep_history, audio=None):
        self.ai_busy.set()
        threading.Thread(target=self._ai_worker, args=(text, keep_history, audio), daemon=True).start()

    def _ai_worker(self, text, keep_history, audio):
        """Запит до AI в окремому потоці, щоб не блокувати розпізнавання."""
        try:
            if audio is not None and self.cfg("dialog_stt") == "whisper":
                better = self.whisper.transcribe(audio) if self.whisper else ""
                if better:
                    log.info(f"[WHISPER] '{better}' (Vosk: '{text}')")
                    text = better

            def on_sentence(sentence):
                if keep_history and not self.dialog_mode:     # діалог завершили, поки чекали
                    self.ai.cancel()
                    return
                self.tts.speak(sentence)

            self.ai.ask(text, on_sentence=on_sentence, keep_history=keep_history,
                        use_tools=bool(self.cfg("ai_tools", True)))
        except AIError as e:
            self.tts.speak(e.user_message)
        except Exception as e:
            log.info(f"[AI] Помилка: {e}")
            self.tts.speak(self.P.get("error"))
        finally:
            self.ai_busy.clear()

    # ── стан ─────────────────────────────────────────────────────────────────
    def set_active(self, value):
        self.is_active = value
        if value:
            self._active_since = time.monotonic()

    def current_state(self):
        if self.muted:
            return "muted"
        if self.tts.busy.is_set():
            return "speaking"
        if self.ai_busy.is_set():
            return "thinking"
        return "dialog" if self.dialog_mode else "active" if self.is_active else "idle"

    def emit_state(self):
        state = self.current_state()
        if state != self._state:
            self._state = state
            log.info(f"[STATE] {state}")      # GUI показує це як індикатор стану

    def activate_by_key(self):
        if self.muted:
            log.info("[HOTKEY] Мікрофон вимкнено в налаштуваннях — ігнорую.")
            return
        if not self.is_active:
            self.set_active(True)
            log.info("[HOTKEY] Активовано — слухаю...")
            self.tts.speak(self.P.get("listening"))

    def stop_speaking(self):
        self.ai.cancel()
        self.tts.stop()
        self.commands.cancel_timers()
        log.info("[HOTKEY] Мовлення перервано.")

    # ── обробка фрази ────────────────────────────────────────────────────────
    def handle_text(self, text, audio=None):
        if self.dialog_mode:
            log.info(f"[ДІАЛОГ] '{text}'")
            stop_phrase = None if any(has_kw(text, w) for w in DIALOG_EXIT_WORDS) else self.commands.dialog_stop(text)
            if stop_phrase is not None or any(has_kw(text, w) for w in DIALOG_EXIT_WORDS):
                self.dialog_mode = False
                self.ai.reset()
                self.tts.speak(stop_phrase or self.P.get("dialog_end"))
            else:
                self._start_ai(text, True, audio)
        elif self.is_active:
            log.info(f"[VOSK] '{text}'")
            self.execute(text)
        elif any(has_kw(text, w) for w in self.wake_words):
            log.info(f"[VOSK] '{text}'\n[АКТИВАЦІЯ] Слухаю команду...")
            if self.commands.has_match(text):
                self.execute(text)                  # напр. "норі відкрий браузер"
            else:
                self.set_active(True)
                self.tts.speak(self.P.get("listening"))

    def execute(self, text):
        self.set_active(False)
        log.info(f"[РОЗПІЗНАНО] '{text}'")
        outcome = self.commands.run(text)
        if outcome.kind == "exit":
            self.tts.speak(outcome.phrase or self.P.get("goodbye"))
            self.tts.wait_idle(10)
            sys.stdout.flush()
            os._exit(0)
        elif outcome.kind == "stop_dialog":     # поза діалогом «стоп» просто перериває мовлення
            self.stop_speaking()
        elif outcome.kind == "dialog":
            if not self.api_key:
                self.tts.speak(self.P.get("no_api_key_dialog"))
                return
            self.ai.reset()
            self.dialog_mode = True
            self.tts.speak(outcome.phrase or self.P.get("dialog_start"))
        elif outcome.kind == "none":
            log.info(f"[НЕ ЗНАЙДЕНО] '{text}'")
            if self.api_key and self.cfg("ai_fallback", True):
                self._start_ai(text, False)         # AI зрозуміє вільне формулювання і викличе функцію
            else:
                self.tts.speak(self.P.get("not_found"))

    # ── зв'язок з GUI (stdin) ────────────────────────────────────────────────
    def _stdin_listener(self):
        """"reload" — перечитати файли, "stop" — перервати мовлення, "phrases" — згенерувати варіанти фраз
        через ШІ, "quit" — вийти.
        Закриття каналу (GUI зник) = вихід, щоб мікрофон не лишався увімкненим."""
        try:
            sys.stdin.reconfigure(errors="replace")
        except Exception:
            pass
        try:
            for line in sys.stdin:
                cmd = line.strip().lower()
                if cmd == "reload":
                    self.reload_config()
                elif cmd == "stop":
                    self.stop_speaking()
                elif cmd == "phrases":
                    self.generate_phrases(force=True)
                elif cmd == "quit":
                    log.info("[СИСТЕМА] Отримано команду завершення.")
                    sys.stdout.flush()
                    os._exit(0)
        except Exception as e:
            log.info(f"[СИСТЕМА] Помилка каналу зв'язку з GUI: {e}")
        log.info("[СИСТЕМА] Зв'язок з GUI втрачено — завершую роботу.")
        sys.stdout.flush()
        os._exit(0)

    def start_stdin_listener(self):
        if os.environ.get("ASISTENT_FROM_GUI") == "1" and sys.stdin is not None:
            threading.Thread(target=self._stdin_listener, daemon=True).start()

    # ── звук ─────────────────────────────────────────────────────────────────
    def audio_callback(self, indata, frames, time_info, status):
        # Поки асистент говорить, чекає AI або мікрофон вимкнено — звук відкидаємо
        if self.muted or self.tts.busy.is_set() or self.ai_busy.is_set():
            return
        self.audio_q.put(bytes(indata))

    @staticmethod
    def _drain(q):
        try:
            while True:
                q.get_nowait()
        except queue.Empty:
            pass

    @staticmethod
    def find_model_dir():
        for d in (common.BASE_DIR, common.RES_DIR, os.getcwd()):
            p = os.path.abspath(os.path.join(d, "model_uk"))
            if os.path.isdir(p):
                return p
        fatal(f"Папка моделі 'model_uk' не знайдена.\nПокладіть її поруч з програмою:\n{common.BASE_DIR}")

    def load_model(self):
        SetLogLevel(-1)
        path = self.find_model_dir()
        try:
            return Model(path)
        except Exception as e:
            if os.name == "nt":      # запасний варіант для шляхів з кирилицею
                import ctypes
                buf = ctypes.create_unicode_buffer(260)
                if ctypes.windll.kernel32.GetShortPathNameW(path, buf, 260):
                    try:
                        return Model(buf.value)
                    except Exception as e2:
                        e = e2
            fatal(f"Не вдалося завантажити модель Vosk:\n{e}\n\nШлях: {path}")

    # ── головний цикл ────────────────────────────────────────────────────────
    def run(self):
        self.load_config(first_run=True)
        self.tts.start()
        self.start_stdin_listener()
        if self.cfg("dialog_stt") == "whisper":
            self.whisper = WhisperSTT(str(self.cfg("whisper_model", "small")))
            threading.Thread(target=self.whisper._load, daemon=True).start()    # прогріваємо у фоні

        model = self.load_model()
        rec = KaldiRecognizer(model, SAMPLE_RATE)
        wake_rec, wake_built = None, ()
        log.info("[VOSK] Модель завантажено.")

        hotkey = str(self.cfg("hotkey", "f10"))
        log.info(f"\nАсистент готовий. Клавіша: {hotkey.upper()} або скажіть '{self.name}' / 'Асистент'\n")
        for key, handler, label in ((hotkey, self.activate_by_key, "активація"),
                                    (str(self.cfg("stop_hotkey", "f9")), self.stop_speaking, "стоп")):
            try:
                keyboard.add_hotkey(key, handler)
            except Exception as e:
                log.info(f"[HOTKEY] Не вдалося зареєструвати {key.upper()} ({label}): {e}")
        self.tts.speak(self.P.get("greeting"))
        timer = threading.Timer(5.0, self.generate_phrases)       # не заважаємо старту
        timer.daemon = True
        timer.start()

        try:
            stream = sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=BLOCK_SIZE, dtype="int16",
                                       channels=1, callback=self.audio_callback)
        except Exception as e:
            fatal("Не вдалося відкрити мікрофон.\nПеревірте, що мікрофон підключений, обраний за "
                  "замовчуванням, і що в Параметри → Конфіденційність → Мікрофон дозволено доступ "
                  f"для класичних програм.\n\nДеталі: {e}")

        was_paused, cur_rec, utt = False, None, bytearray()
        try:
            with stream:
                while True:
                    try:
                        data = self.audio_q.get(timeout=0.3)
                    except queue.Empty:
                        data = None
                    self.emit_state()
                    self.check_phrase_file()

                    if self.is_active and time.monotonic() - self._active_since > float(self.cfg("active_timeout", 10)):
                        self.set_active(False)
                        log.info("[АКТИВАЦІЯ] Команди не було — повертаюсь у режим очікування.")

                    # Мовлення / запит до AI / вимкнений мікрофон: нічого не розпізнаємо
                    if self.muted or self.tts.busy.is_set() or self.ai_busy.is_set():
                        self._drain(self.audio_q)
                        utt.clear()
                        was_paused = True
                        continue

                    # Експеримент: у режимі очікування слухаємо лише слово активації (вузька граматика)
                    use_wake = bool(self.cfg("wake_grammar", False)) and not self.is_active and not self.dialog_mode
                    if use_wake and (wake_rec is None or wake_built != self.wake_words):
                        try:
                            wake_rec = KaldiRecognizer(model, SAMPLE_RATE,
                                                       json.dumps(list(self.wake_words) + ["[unk]"], ensure_ascii=False))
                            wake_built = self.wake_words
                        except Exception as e:
                            log.info(f"[VOSK] Граматика слова активації недоступна ({e}); вимикаю.")
                            self.settings["wake_grammar"] = False
                            use_wake = False
                    active_rec = wake_rec if use_wake else rec
                    if was_paused or active_rec is not cur_rec:
                        try:
                            active_rec.Reset()      # викидаємо напіврозпізнаний «хвіст»
                        except Exception:
                            pass
                        cur_rec, was_paused = active_rec, False
                    if data is None:
                        continue

                    collect = self.dialog_mode and self.cfg("dialog_stt") == "whisper"
                    if collect:
                        utt += data
                        if len(utt) > MAX_UTTERANCE_BYTES:
                            del utt[:len(utt) - MAX_UTTERANCE_BYTES]
                    if not active_rec.AcceptWaveform(data):
                        continue
                    text = json.loads(active_rec.Result()).get("text", "").replace("[unk]", "").strip().lower()
                    audio = bytes(utt) if collect else None
                    utt.clear()
                    if text:
                        self.handle_text(text, audio)
        except KeyboardInterrupt:
            log.info("\nЗавершення роботи.")
            os._exit(0)
        except Exception as e:
            fatal(f"Несподівана помилка:\n{e}")


def main():
    Assistant().run()


if __name__ == "__main__":
    main()
