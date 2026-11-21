"""Озвучення: SAPI (офлайн) або edge-tts (онлайн, якісніший голос).
Говорить окремий потік; текст ділиться на речення, тож мовлення можна перервати (stop)."""
import os
import queue
import threading
import time
import logging

from uktext import clean_for_speech, split_sentences

log = logging.getLogger("asistent")


def _msgbox(title, text, icon=0x30):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, icon)
    except Exception:
        pass


def _play_mp3(path, should_stop, volume):
    """Програє mp3 через системний MCI (Windows) без додаткових бібліотек."""
    import ctypes
    mci = ctypes.windll.winmm.mciSendStringW
    alias = "asistent_tts"
    mci(f'open "{path}" type mpegvideo alias {alias}', None, 0, 0)
    try:
        mci(f"setaudio {alias} volume to {int(max(0, min(100, volume)) * 10)}", None, 0, 0)
        mci(f"play {alias}", None, 0, 0)
        buf = ctypes.create_unicode_buffer(64)
        while not should_stop():
            mci(f"status {alias} mode", buf, 63, 0)
            if buf.value != "playing":
                break
            time.sleep(0.05)
    finally:
        mci(f"stop {alias}", None, 0, 0)
        mci(f"close {alias}", None, 0, 0)


class TTS:
    def __init__(self, cfg):
        """cfg(key, default) — читач налаштувань; значення беруться щоразу наново,
        тому гучність, голос і швидкість змінюються на льоту."""
        self.cfg = cfg
        self.busy = threading.Event()      # поки асистент говорить — мікрофон ігноруємо
        self._q = queue.Queue()
        self._gen = 0                      # stop() збільшує: усе «старше» кидаємо
        self._speaker = None
        self._voice_key = None
        self._voice_warned = False
        self._edge_down_until = 0.0
        self._edge_warned = False

    # ── публічне ──────────────────────────────────────────────────────────────
    def start(self):
        threading.Thread(target=self._run, daemon=True, name="tts").start()

    def speak(self, text):
        text = clean_for_speech(text)
        if not text:
            return
        log.info(f"[TTS] >>> {text}")
        self.busy.set()
        gen = self._gen
        for sentence in split_sentences(text):
            self._q.put((gen, sentence))

    def stop(self):
        """Перервати поточне мовлення і викинути чергу."""
        self._gen += 1
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

    def wait_idle(self, timeout=10.0):
        deadline = time.monotonic() + timeout
        while self.busy.is_set() and time.monotonic() < deadline:
            time.sleep(0.05)

    # ── потік ─────────────────────────────────────────────────────────────────
    def _init_sapi(self):
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()          # COM має бути ініціалізований саме в цьому потоці
            self._speaker = win32com.client.Dispatch("SAPI.SpVoice")
        except Exception as e:
            log.info(f"[TTS] Не вдалося запустити синтезатор SAPI: {e}")
            self._speaker = None

    def _run(self):
        self._init_sapi()
        while True:
            gen, text = self._q.get()
            self.busy.set()
            try:
                if gen == self._gen:
                    self._say(gen, text)
            except Exception as e:
                log.info(f"[TTS] Помилка: {e}")
            finally:
                if self._q.empty():
                    time.sleep(float(self.cfg("echo_tail", 0.3)))
                    if self._q.empty():
                        self.busy.clear()

    def _say(self, gen, text):
        if self.cfg("tts_backend", "sapi") == "edge" and time.monotonic() >= self._edge_down_until:
            try:
                self._say_edge(gen, text)
                return
            except Exception as e:
                self._edge_down_until = time.monotonic() + 60     # не мучимо мережу щосекунди
                if not self._edge_warned:
                    self._edge_warned = True
                    log.info(f"[TTS] edge-tts недоступний ({e}); використовую SAPI.")
        if gen == self._gen:
            self._say_sapi(gen, text)

    # ── SAPI ──────────────────────────────────────────────────────────────────
    def _volume(self):
        try:
            return max(0, min(100, int(self.cfg("volume", 100))))
        except (TypeError, ValueError):
            return 100

    def _pick_ukrainian_voice(self, sp):
        voices = sp.GetVoices()
        for i in range(voices.Count):
            v = voices.Item(i)
            info = v.GetDescription().lower()
            try:
                info += " " + v.GetAttribute("Language")      # 422 = українська
            except Exception:
                pass
            if any(k in info for k in ("ukr", "marianna", "422")):
                sp.Voice = v
                return True
        return False

    def _apply_voice(self, sp, voice_id, voice_name):
        if voice_id or voice_name:
            try:
                voices = sp.GetVoices()
                for i in range(voices.Count):
                    v = voices.Item(i)
                    if (voice_id and v.Id == voice_id) or (voice_name and v.GetDescription() == voice_name):
                        sp.Voice = v
                        log.info(f"[TTS] Голос: {voice_name or voice_id}")
                        return
                if voice_id:        # голоси, яких SAPI не перелічує (наприклад, OneCore)
                    import win32com.client
                    token = win32com.client.Dispatch("SAPI.SpObjectToken")
                    token.SetId(voice_id)
                    sp.Voice = token
                    log.info(f"[TTS] Голос: {voice_name or voice_id}")
                    return
            except Exception as e:
                log.info(f"[TTS] Не вдалося застосувати голос '{voice_name or voice_id}': {e}")
        try:
            if self._pick_ukrainian_voice(sp):
                return
        except Exception:
            pass
        if not self._voice_warned:
            self._voice_warned = True
            log.info("[TTS] Українського голосу не знайдено, використовується стандартний.")
            threading.Thread(target=_msgbox, daemon=True, args=(
                "Асистент — увага",
                "У Windows не встановлено український голос.\nМовлення може звучати некоректно.\n\n"
                "Встановіть його: Параметри → Час і мова → Мовлення → Додати голоси.")).start()

    def _say_sapi(self, gen, text):
        sp = self._speaker
        if sp is None:
            return
        key = (str(self.cfg("tts_model_id", "") or ""), str(self.cfg("tts_model", "") or ""))
        if key != self._voice_key:
            self._voice_key = key
            self._apply_voice(sp, *key)
        try:
            sp.Rate = max(-10, min(10, int(self.cfg("tts_rate", 2))))
        except (TypeError, ValueError):
            pass
        sp.Volume = self._volume()
        sp.Speak(text, 1)                        # асинхронно, щоб можна було перервати
        while not sp.WaitUntilDone(80):
            if gen != self._gen:
                sp.Speak("", 3)                  # 1 (async) | 2 (purge): обірвати мовлення
                break

    # ── edge-tts ──────────────────────────────────────────────────────────────
    def _say_edge(self, gen, text):
        import asyncio
        import tempfile
        import edge_tts
        voice = str(self.cfg("edge_voice", "uk-UA-PolinaNeural"))
        rate = f"{int(self.cfg('edge_rate', 0)):+d}%"
        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        try:
            asyncio.run(edge_tts.Communicate(text, voice, rate=rate).save(path))
            if gen == self._gen:
                _play_mp3(path, lambda: gen != self._gen, self._volume())
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
