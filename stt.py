"""Необов'язкове розпізнавання через faster-whisper — для діалогу з AI (набагато точніше за малу
модель Vosk). Vosk лишається «слухачем» і ріже звук на фрази, а Whisper розшифровує їх."""
import logging

log = logging.getLogger("asistent")


class WhisperSTT:
    def __init__(self, model_size="small"):
        self.model_size = model_size
        self._model = None
        self._np = None
        self.failed = False

    def _load(self):
        if self._model is not None or self.failed:
            return
        try:
            import numpy
            from faster_whisper import WhisperModel
            log.info(f"[WHISPER] Завантаження моделі '{self.model_size}' (перший раз — з інтернету)...")
            self._np = numpy
            self._model = WhisperModel(self.model_size, device="cpu", compute_type="int8")
            log.info("[WHISPER] Модель готова.")
        except Exception as e:
            self.failed = True
            log.info(f"[WHISPER] Недоступний ({e}); використовую Vosk.")

    def transcribe(self, pcm, language="uk"):
        """pcm — сирі 16-бітні моно семпли 16 кГц. Порожній рядок, якщо не вийшло."""
        self._load()
        if self._model is None or len(pcm) < 3200:       # коротше 0.1 с
            return ""
        try:
            audio = self._np.frombuffer(pcm, dtype=self._np.int16).astype(self._np.float32) / 32768.0
            segments, _ = self._model.transcribe(audio, language=language, beam_size=1,
                                                 condition_on_previous_text=False)
            return " ".join(s.text.strip() for s in segments).strip().lower()
        except Exception as e:
            log.info(f"[WHISPER] Помилка розпізнавання: {e}")
            return ""
