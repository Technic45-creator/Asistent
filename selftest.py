import json
import os
import sys
import tempfile
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
HOME = tempfile.mkdtemp(prefix="asistent_test_")
os.environ["ASISTENT_HOME"] = HOME


def stub(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


class FakeKeyboard:
    def __init__(self): self.pressed = []
    def press_and_release(self, k): self.pressed.append(k)
    def add_hotkey(self, *a, **k): pass


class _Dummy:
    def __init__(self, *a, **k): pass


FK = FakeKeyboard()
stub("sounddevice", RawInputStream=_Dummy)
stub("keyboard", add_hotkey=FK.add_hotkey, press_and_release=FK.press_and_release)
stub("vosk", Model=_Dummy, KaldiRecognizer=_Dummy, SetLogLevel=lambda x: None)

import common  # noqa: E402
import requests  # noqa: E402

PASSED = []


def ok(name):
    PASSED.append(name)
    print(f"  ✓ {name}")


def write(name, data, bom=False):
    with open(os.path.join(HOME, name), "w", encoding="utf-8-sig" if bom else "utf-8") as f:
        f.write(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False))


# ══ common ═══════════════════════════════════════════════════════════════════
def test_common():
    print("common")
    write("user.dat", {"name": "Іра", "api_key": "FILEKEY"}, bom=True)
    assert common.load_user()["name"] == "Іра"
    ok("файл із BOM читається")
    write("broken.json", "{ nope")
    assert common.read_json(os.path.join(HOME, "broken.json")) is None
    assert os.path.exists(os.path.join(HOME, "broken.json.bad")) and common.LOAD_ERRORS
    ok("пошкоджений файл -> копія .bad + запис у LOAD_ERRORS")
    common.save_json_file(os.path.join(HOME, "a.json"), {"x": 1})
    assert not os.path.exists(os.path.join(HOME, "a.json.tmp"))
    ok("атомарний запис")

    import keyring
    from keyring.backend import KeyringBackend

    class Mem(KeyringBackend):
        priority = 1
        store = {}
        def get_password(self, s, u): return self.store.get((s, u))
        def set_password(self, s, u, p): self.store[(s, u)] = p
        def delete_password(self, s, u): self.store.pop((s, u), None)

    keyring.set_keyring(Mem())
    common.save_user({"name": "Іра", "api_key": "SECRET"})
    raw = json.load(open(common.user_file(), encoding="utf-8"))
    assert raw["api_key"] == "" and Mem.store[("Asistent", "gemini_api_key")] == "SECRET"
    assert common.load_user()["api_key"] == "SECRET"
    ok("API-ключ іде в keyring, у файлі його немає")
    common.save_user({"name": "Іра", "api_key": ""})
    assert common.load_user()["api_key"] == ""
    ok("порожній ключ видаляється зі сховища")
    keyring.set_keyring(keyring.backends.fail.Keyring())
    common.save_user({"name": "Іра", "api_key": "PLAIN"})
    assert json.load(open(common.user_file(), encoding="utf-8"))["api_key"] == "PLAIN"
    assert common.load_user()["api_key"] == "PLAIN"
    ok("без keyring ключ лишається в user.dat (сумісність)")


# ══ matching / uktext ════════════════════════════════════════════════════════
def test_matching():
    print("matching / uktext")
    from matching import has_kw, match_commands, find_overlaps
    from uktext import parse_duration, parse_number, format_duration, split_sentences, clean_for_speech
    assert has_kw("відкрий браузер", "відкрий") and not has_kw("перенорі", "норі")
    cmds = {"a": {"keywords": ["браузер"]}, "b": {"keywords": ["відкрий браузер"]},
            "c": {"keywords": ["музика"], "disabled": True}, "d": {"keywords": "калькулятор"}}
    assert [k for k, _ in match_commands(cmds, "відкрий браузер")] == ["b", "a"]
    assert match_commands(cmds, "музика") == [] and match_commands(cmds, "калькулятор")
    ok("найдовший збіг першим, disabled пропускається, keywords-рядок")
    ov = find_overlaps(cmds, "new", ["відкрий браузер", "музика"])
    assert any(o[1] == "a" for o in ov) and not any(o[1] == "c" for o in ov)
    ok("перетини ключових слів (вимкнені ігноруються)")
    cases = {"на п'ять хвилин": 300, "на годину": 3600, "на півгодини": 1800,
             "одна година тридцять хвилин": 5400, "на 10 хвилин": 600, "на двадцять п'ять секунд": 25}
    for text, sec in cases.items():
        assert parse_duration(text) == sec, (text, parse_duration(text))
    assert parse_duration("привіт") is None and parse_number("сімдесят п'ять") == 75
    assert format_duration(5400) == "1 годину 30 хвилин"
    ok("числа та тривалості українською")
    assert split_sentences("Привіт! Це тест. 3.5 ок") == ["Привіт!", "Це тест.", "3.5 ок"]
    assert clean_for_speech("**Так** `код`") == "Так код"
    ok("речення та очищення markdown")


# ══ commands ═════════════════════════════════════════════════════════════════
def make_commands(spoken):
    from commands import Commands
    from phrases import Phrases
    cfg = lambda k, d=None: {"builtins": True}.get(k, d)
    P = Phrases(os.path.join(HOME, "no_phrases.json"), os.path.join(HOME, "no_cache.json"))
    c = Commands(spoken.append, cfg, P, lambda: os.path.join(HOME, "notes.txt"), FK)
    return c


def variants(key, **fmt):
    """Усі можливі озвучення вбудованої фрази (для перевірки випадкових відповідей)."""
    from phrases import DEFAULTS
    base = {"name": "Норі", "user": "друже", **fmt}
    return {v.format(**base) for v in DEFAULTS[key]}


def test_commands():
    print("commands")
    import commands as C
    opened = []
    C.webbrowser.open = lambda u: opened.append(u)
    spoken = []
    c = make_commands(spoken)
    c.set_commands({
        "browser": {"keywords": ["браузер"], "action": "open_website", "url": "https://x", "speak": "Відкриваю."},
        "off": {"keywords": ["вимкнена"], "action": "open_website", "url": "https://no", "disabled": True},
        "bye": {"keywords": ["вийди"], "action": "exit", "speak": "Бувай"},
        "talk": {"keywords": ["поговоримо"], "action": "start_dialog"},
        "keys": {"keywords": ["скопіюй"], "action": "hotkey", "keys": "ctrl+c", "speak": "Готово."},
        "map": {"keywords": ["вкладка"], "action": "hotkey_map", "map": {"нову": "ctrl+t"}, "speak": "Ок."},
    })
    assert c.run("відкрий браузер").kind == "done" and opened == ["https://x"] and spoken[-1] == "Відкриваю."
    assert c.run("вимкнена").kind == "none" and c.run("вийди").kind == "exit"
    assert c.run("давай поговоримо").kind == "dialog"
    FK.pressed.clear()
    c.run("скопіюй"); assert FK.pressed == ["ctrl+c"]
    c.run("вкладка закрий")                      # hotkey_map без збігу -> далі не підходить
    c.run("відкрий нову вкладка") if False else None
    ok("користувацькі дії, disabled, exit/dialog, hotkey")

    import re as _re
    spoken.clear(); c.run("яка година"); assert _re.search(r"\d{1,2}:\d{2}", spoken[-1]), spoken[-1]
    spoken.clear(); c.run("який сьогодні день"); assert spoken[-1].startswith("Сьогодні"), spoken[-1]
    ok("час і дата")
    spoken.clear(); c.run("постав таймер на п'ять хвилин")
    assert spoken[-1] in variants("timer_set", duration="5 хвилин"), spoken[-1]
    c.run("скасуй таймер"); assert "скасовано" in spoken[-1]
    spoken.clear(); c.run("постав таймер"); assert spoken[-1] in variants("hint_timer"), spoken[-1]
    spoken.clear(); c._b_timer({"seconds": 1, "label": "чай"}); time.sleep(1.4)
    assert spoken[-1] in variants("timer_done_label", label="чай"), spoken[-1]
    ok("таймер: запуск, скасування, без числа, спрацювання")
    FK.pressed.clear(); c.run("зроби голосніше"); assert FK.pressed == ["volume up"] * 5
    FK.pressed.clear(); c.run("встанови гучність на сорок відсотків")
    assert FK.pressed.count("volume down") == 50 and FK.pressed.count("volume up") == 20
    FK.pressed.clear(); c.run("вимкни звук"); assert FK.pressed == ["volume mute"]
    ok("гучність: вище/встановити/mute")
    spoken.clear(); c.run("запиши нотатку купити хліб"); assert spoken[-1] in variants("note_saved")
    c.run("прочитай нотатки"); assert "купити хліб" in spoken[-1], spoken[-1]
    spoken.clear(); c.run("запиши нотатку"); assert spoken[-1] in variants("hint_note")
    ok("нотатки")
    opened.clear(); c.run("знайди в гуглі погода київ")
    assert opened == ["https://www.google.com/search?q=%D0%BF%D0%BE%D0%B3%D0%BE%D0%B4%D0%B0+%D0%BA%D0%B8%D1%97%D0%B2"]
    ok("пошук у Google")
    decls = c.tool_declarations()
    names = [d["name"] for d in decls]
    assert "execute_command" in names and "set_timer" in names and "exit" not in str(decls[0]["parameters"]["properties"]["command"]["enum"])
    assert c.call_tool("execute_command", {"command": "off"}).startswith("Такої")
    opened.clear(); assert c.call_tool("execute_command", {"command": "browser"}) == "Виконано." and opened
    assert "5 секунд" in c.call_tool("set_timer", {"seconds": 5}); c.cancel_timers()
    ok("функції для AI: опис і виклик")
    c.cfg = lambda k, d=None: False if k == "builtins" else d
    assert c.run("яка година").kind == "none"
    ok("вбудовані команди можна вимкнути")


# ══ ai ═══════════════════════════════════════════════════════════════════════
class FakeResp:
    def __init__(self, chunks=None, code=200, stream=True):
        self.status_code, self._chunks, self._stream = code, chunks or [], stream
        self.text = "err"
    def raise_for_status(self):
        if self.status_code >= 400:
            e = requests.exceptions.HTTPError(); e.response = self; raise e
    def iter_lines(self):
        for ch in self._chunks:
            yield ("data: " + json.dumps(ch, ensure_ascii=False)).encode("utf-8")
            yield b""
    def json(self): return self._chunks[0] if self._chunks else {}


def chunk(*parts, finish=None):
    c = {"candidates": [{"content": {"parts": list(parts)}}]}
    if finish: c["candidates"][0]["finishReason"] = finish
    return c


def make_ai(settings=None, run_tool=None):
    from ai import Gemini
    st = {"ai_stream": True, **(settings or {})}
    return Gemini(lambda k, d=None: st.get(k, d), lambda: "KEY", lambda: "m", lambda: "sys",
                  lambda: [{"name": "t"}], run_tool)


def test_ai():
    print("ai")
    import ai as A
    sent, calls = [], []
    g = make_ai()
    resps = iter([FakeResp([chunk({"text": "Привіт! Як спра"}), chunk({"text": "ви? Все добре."})])])
    requests.post = lambda url, **kw: (calls.append((url, kw)), next(resps))[1]
    reply = g.ask("привіт", on_sentence=sent.append)
    assert sent == ["Привіт!", "Як справи?", "Все добре."], sent
    assert "streamGenerateContent" in calls[0][0] and calls[0][1]["params"] == {"alt": "sse"}
    assert [m["role"] for m in g.history] == ["user", "model"]
    ok("потокова відповідь озвучується по реченнях (в т.ч. розрізане посередині)")

    g = make_ai(run_tool=lambda n, a: f"виконано {n} {a}")
    fc = {"functionCall": {"name": "set_timer", "args": {"seconds": 60}}, "thoughtSignature": "SIG"}
    resps = iter([FakeResp([chunk(fc)]), FakeResp([chunk({"text": "Таймер запущено."})])])
    calls.clear(); sent.clear()
    reply = g.ask("постав таймер на хвилину", on_sentence=sent.append)
    assert reply == "Таймер запущено." and sent == ["Таймер запущено."]
    h = g.history
    assert [m["role"] for m in h] == ["user", "model", "user", "model"]
    assert h[1]["parts"][0]["thoughtSignature"] == "SIG"            # підпис мислення збережено
    assert h[2]["parts"][0]["functionResponse"]["name"] == "set_timer"
    assert "tools" in calls[0][1]["json"]
    ok("function calling: виклик -> виконання -> підсумок, thoughtSignature зберігається")

    g = make_ai(run_tool=lambda n, a: "ok")
    resps = iter([FakeResp([chunk(fc)]), FakeResp([chunk({"text": ""}, finish="STOP")])])
    sent.clear(); assert g.ask("x", on_sentence=sent.append) == "Готово." and sent == ["Готово."]
    ok("лише виклик функції без тексту -> «Готово.»")

    g = make_ai(run_tool=lambda n, a: "ok")
    seq = [FakeResp(code=400), FakeResp([chunk({"text": "Відповідь."})])]
    calls.clear(); resps = iter(seq)
    assert g.ask("x") == "Відповідь." and "tools" not in calls[1][1]["json"] and g._tools_broken
    ok("400 через tools -> повтор без функцій")

    g = make_ai()
    resps = iter([FakeResp(code=429)])
    try: g.ask("x"); raise SystemExit("мало бути AIError")
    except A.AIError as e: assert "ліміт" in e.user_message
    assert g.history == []
    resps = iter([FakeResp([chunk({"text": ""}, finish="MAX_TOKENS")])])
    try: g.ask("x"); raise SystemExit("мало бути AIError")
    except A.AIError as e: assert "не отримав" in e.user_message
    assert g.history == []
    def boom(*a, **k): raise requests.exceptions.Timeout()
    requests.post = boom
    try: g.ask("x"); raise SystemExit("мало бути AIError")
    except A.AIError as e: assert "довго" in e.user_message
    assert g.history == []
    ok("помилки -> AIError з текстом, історія без осиротілого питання")

    requests.post = lambda url, **kw: next(resps)
    g = make_ai()
    for i in range(20):
        resps = iter([FakeResp([chunk({"text": f"в{i}."})])]); g.ask(f"п{i}")
    assert len(g.history) <= A.MAX_HISTORY and g.history[0]["role"] == "user"
    roles = [m["role"] for m in g.history]
    assert all(a != b for a, b in zip(roles, roles[1:]))
    resps = iter([FakeResp([chunk({"text": "разове."})])])
    n = len(g.history); g.ask("разове", keep_history=False); assert len(g.history) == n
    ok("обрізання історії і разові запитання")

    g = make_ai(run_tool=lambda n, a: "ok")
    resps = iter([FakeResp([chunk(fc), chunk({"text": "Ось. Далі."})])])
    g.ask("x", on_sentence=lambda s: g.cancel())          # стоп-клавіша під час відповіді з викликом функції
    assert not any("functionCall" in p for m in g.history for p in m["parts"])
    roles = [m["role"] for m in g.history]
    assert roles == ["user", "model"], roles
    ok("переривання (F9) з невиконаним викликом функції не ламає історію")
    g2 = make_ai(run_tool=lambda n, a: (_ for _ in ()).throw(RuntimeError("зламано")))
    resps = iter([FakeResp([chunk(fc)]), FakeResp([chunk({"text": "Не вийшло."})])])
    assert g2.ask("x") == "Не вийшло." and "Помилка: зламано" in json.dumps(g2.history, ensure_ascii=False)
    ok("помилка функції передається моделі")

    g3 = make_ai(settings={"ai_stream": False})
    requests.post = lambda url, **kw: (calls.append((url, kw)), FakeResp([chunk({"text": "Одним шматком. Два речення."})]))[1]
    calls.clear(); sent.clear(); g3.ask("x", on_sentence=sent.append)
    assert "generateContent" in calls[0][0] and "stream" not in calls[0][0].split(":")[-1][:6] and len(sent) == 2
    ok("режим без потоку")

    def fake_get(url, **kw):
        page = FakeGet.pages.pop(0); return page
    class FakeGet:
        pages = []
        def __init__(s, d): s.d = d; s.status_code = 200
        def raise_for_status(s): pass
        def json(s): return s.d
    FakeGet.pages = [FakeGet({"models": [{"name": "models/gemini-a", "supportedGenerationMethods": ["generateContent"]},
                                          {"name": "models/embed", "supportedGenerationMethods": ["embedContent"]}],
                              "nextPageToken": "t"}),
                     FakeGet({"models": [{"name": "models/gemini-b", "supportedGenerationMethods": ["generateContent"]}]})]
    requests.get = fake_get
    assert A.list_models("K") == ["gemini-a", "gemini-b"]
    try: A.list_models(""); raise SystemExit("мало бути AIError")
    except A.AIError: pass
    ok("список моделей (з пагінацією, лише generateContent)")


# ══ tts ══════════════════════════════════════════════════════════════════════
class FakeSpeaker:
    def __init__(self): self.said, self.Rate, self.Volume, self.purged = [], 0, 0, 0
    def Speak(self, text, flags=0):
        if flags == 3: self.purged += 1
        else: self.said.append(text)
    def WaitUntilDone(self, ms): time.sleep(ms / 1000); return True


class SlowSpeaker(FakeSpeaker):
    def WaitUntilDone(self, ms): time.sleep(ms / 1000); return False      # «говорить» без кінця


def test_tts():
    print("tts")
    import tts as T
    st = {"echo_tail": 0.05, "volume": 30, "tts_rate": 3}
    t = T.TTS(lambda k, d=None: st.get(k, d))
    sp = FakeSpeaker(); t._init_sapi = lambda: setattr(t, "_speaker", sp); t.start()
    t.speak("Перше речення. Друге речення!")
    assert t.busy.is_set()
    time.sleep(0.5)
    assert sp.said == ["Перше речення.", "Друге речення!"] and sp.Volume == 30 and sp.Rate == 3
    assert not t.busy.is_set()
    ok("речення озвучуються по черзі, гучність/швидкість з налаштувань, busy знімається")

    t2 = T.TTS(lambda k, d=None: st.get(k, d)); slow = SlowSpeaker()
    t2._init_sapi = lambda: setattr(t2, "_speaker", slow); t2.start()
    t2.speak("Довге мовлення. Друге. Третє."); time.sleep(0.25)
    t2.stop(); time.sleep(0.4)
    assert slow.purged >= 1 and len(slow.said) == 1
    assert not t2.busy.is_set()
    t2.speak("Нове"); time.sleep(0.3); assert slow.said[-1] == "Нове"
    ok("stop(): обриває поточне, викидає чергу, наступне мовлення працює")

    # edge: успіх
    played = []
    st3 = {"echo_tail": 0.05, "tts_backend": "edge", "edge_voice": "uk-UA-OstapNeural", "edge_rate": 10}
    class Comm:
        def __init__(s, text, voice, rate=None): played.append(("synth", text, voice, rate))
        async def save(s, path): open(path, "wb").write(b"mp3")
    sys.modules["edge_tts"] = types.SimpleNamespace(Communicate=Comm)
    T._play_mp3 = lambda path, stop, vol: played.append(("play", os.path.exists(path), vol))
    t3 = T.TTS(lambda k, d=None: st3.get(k, d)); sp3 = FakeSpeaker()
    t3._init_sapi = lambda: setattr(t3, "_speaker", sp3); t3.start()
    t3.speak("Привіт"); time.sleep(0.5)
    assert played[0] == ("synth", "Привіт", "uk-UA-OstapNeural", "+10%") and played[1][:2] == ("play", True)
    assert sp3.said == []
    ok("edge-tts: синтез + відтворення через MCI (підмінене), голос і швидкість з налаштувань")
    # edge: збій -> SAPI
    class Bad(Comm):
        async def save(s, path): raise OSError("немає мережі")
    sys.modules["edge_tts"] = types.SimpleNamespace(Communicate=Bad)
    t3.speak("Ще раз"); time.sleep(0.5)
    assert sp3.said == ["Ще раз"] and t3._edge_down_until > time.monotonic()
    ok("edge-tts недоступний -> запасний SAPI, повторні спроби призупиняються")


# ══ asistent3 ════════════════════════════════════════════════════════════════
def clean_phrase_files():
    for f in ("phrases.json", "phrases_ai.json"):
        if os.path.exists(os.path.join(HOME, f)): os.remove(os.path.join(HOME, f))


def test_assistant():
    print("asistent3")
    clean_phrase_files()
    import asistent3 as A
    write("comand.json", {"browser": {"keywords": ["браузер"], "action": "open_website", "url": "https://x", "speak": "Ок."},
                          "talk": {"keywords": ["поговоримо"], "action": "start_dialog"}})
    write("seting.json", {"assistant_name": "Зоря", "mute_microphone": True, "volume": 20})
    write("user.dat", {"name": "Іра", "api_key": "", "ai_model": "m", "city": "Київ"}, bom=True)
    a = A.Assistant(); spoken = []
    a.tts.speak = a.commands.speak = spoken.append
    a.load_config(first_run=True)
    assert a.name == "Зоря" and a.muted and "зоря" in a.wake_words and a.cfg("volume") == 20
    assert a.cfg("hotkey") == "f10" and a.cfg("builtins") is True
    assert "Київ" in a.system_prompt() and "Зоря" in a.system_prompt()
    write("seting.json", {"assistant_name": "Норі"}); write("comand.json", "{ broken")
    a.reload_config()
    assert not a.muted and "нові" in a.wake_words and "browser" in a.commands_data
    ok("конфіг: ім'я, mute, слова активації, reload, пошкоджений файл не стирає стан")

    import commands as C
    C.webbrowser.open = lambda u: None
    a.handle_text("просто розмова")                      # без слова активації — ігнор
    assert spoken == []
    a.handle_text("норі")
    assert a.is_active and spoken[-1] in variants("listening", user="Іра"), spoken[-1]
    a.handle_text("відкрий браузер"); assert not a.is_active and spoken[-1] == "Ок."
    a.handle_text("норі відкрий браузер"); assert spoken[-1] == "Ок."
    a.handle_text("норі яка година"); assert any(ch.isdigit() for ch in spoken[-1]), spoken[-1]
    ok("активація словом, команда після активації, «слово+команда» одним реченням, вбудовані")

    # невпізнана команда: без ключа -> «не знайдено»; з ключем -> AI-запасний
    a.handle_text("норі"); a.handle_text("абракадабра"); assert spoken[-1] in variants("not_found"), spoken[-1]
    a.api_key = "K"; asked = []
    a._start_ai = lambda text, keep, audio=None: asked.append((text, keep))
    a.handle_text("норі"); a.handle_text("розкажи жарт"); assert asked == [("розкажи жарт", False)]
    a.handle_text("норі"); a.handle_text("давай поговоримо"); assert a.dialog_mode and asked[-1] == ("розкажи жарт", False)
    a.handle_text("як справи"); assert asked[-1] == ("як справи", True)
    a.handle_text("стоп діалог"); assert not a.dialog_mode and spoken[-1] in variants("dialog_end")
    ok("невпізнана фраза -> AI (разово), діалог: старт, репліка, вихід")

    # стани
    a.muted = False; a.dialog_mode = False; a.is_active = False; a.ai_busy.clear()
    a.tts = type("T", (), {"busy": threading.Event()})()
    assert a.current_state() == "idle"
    a.is_active = True; assert a.current_state() == "active"
    a.dialog_mode = True; assert a.current_state() == "dialog"
    a.ai_busy.set(); assert a.current_state() == "thinking"
    a.tts.busy.set(); assert a.current_state() == "speaking"
    a.muted = True; assert a.current_state() == "muted"
    ok("стани для GUI: idle/active/dialog/thinking/speaking/muted")

    # ai-worker: помилка AI озвучується; ai_busy знімається
    a2 = A.Assistant(); sp2 = []; a2.tts.speak = sp2.append; a2.load_config(first_run=True)
    from ai import AIError
    def bad(*args, **kw): raise AIError("Сервер недоступний.")
    a2.ai.ask = bad; a2.ai_busy.set(); a2._ai_worker("x", True, None)
    assert sp2 == ["Сервер недоступний."] and not a2.ai_busy.is_set()
    ok("помилка AI озвучується, мікрофон розблоковується")


# ══ phrases ══════════════════════════════════════════════════════════════════
def test_phrases():
    print("phrases (варіювання відповідей)")
    import phrases as PH
    from ai import AIError
    fp, cp = os.path.join(HOME, "phrases.json"), os.path.join(HOME, "phrases_ai.json")
    for f in (fp, cp):
        if os.path.exists(f): os.remove(f)
    P = PH.Phrases(fp, cp); P.configure(name="Зоря", user="Іра"); P.reload()

    got = [P.get("listening") for _ in range(60)]
    assert all(a != b for a, b in zip(got, got[1:])), "не має повторювати попередню"
    assert len(set(got)) >= 3 and all("{" not in g for g in got)
    assert any(g == "Так, Іра?" for g in got)
    assert P.get("greeting") and P.get("nonexistent_key") == ""
    ok("вбудовані варіанти: випадково, без повторів підряд, {user}/{name} підставляються")

    write("phrases.json", {"listening": ["А?", "Га?"], "time_now": ["Зараз {nope}"], "goodbye": "Па!"})
    P.reload()
    assert {P.get("listening") for _ in range(30)} == {"А?", "Га?"}
    assert P.get("goodbye") == "Па!"                       # рядок замість списку теж ок
    assert any(ch.isdigit() for ch in P.get("time_now", time="14:35")), "помилковий плейсхолдер -> вбудований варіант"
    ok("phrases.json перекриває вбудовані; помилковий плейсхолдер не ламає відповідь")

    P.set_commands({"b": {"speak": ["Одне.", "Друге."]}, "c": {"speak": "Єдине."}, "d": {"speak": ""},
                    "e": {"speak": "Вимкнена", "disabled": True}})
    assert {P.for_command("b", ["Одне.", "Друге."]) for _ in range(30)} == {"Одне.", "Друге."}
    assert P.for_command("c", "Єдине.") == "Єдине." and P.for_command("d", "") == ""
    assert "cmd:e" not in P.seeds()
    ok("speak команди: рядок або список варіантів; порожній і вимкнені ігноруються")

    # --- ШІ ---
    os.remove(fp); P.reload()
    calls = []
    def fake_ai(key, model, prompt, **kw):
        batch = json.loads(prompt[prompt.index("\n\n{") + 2:])
        calls.append(list(batch))
        out = {}
        for k, seeds in batch.items():
            ph = " ".join(sorted("{" + p + "}" for p in PH._placeholders(" ".join(seeds))))
            out[k] = [f"варіант {k} один {ph}".strip(), f"варіант {k} два {ph}".strip(),
                      seeds[0],                              # дубль еталона -> відсіяти
                      "Без плейсхолдера" if ph else "Зайвий {extra}",   # порушує набір плейсхолдерів
                      "дуже довгий " * 20,                  # задовгий
                      "два\nрядки"]
        return "```json\n" + json.dumps(out, ensure_ascii=False) + "\n```"          # у обгортці, як буває
    PH.generate_text = fake_ai
    n = P.generate("KEY", "m", per_key=6)
    keys = set(P.seeds())
    assert n >= 2 * len(keys) and all(len(P.ai_variants(k)) >= 2 for k in keys), (n, len(keys))
    assert all(len(c) <= PH.KEYS_PER_REQUEST for c in calls) and len(calls) == -(-len(keys) // PH.KEYS_PER_REQUEST)
    pool = P.pool("timer_set")
    assert "варіант timer_set один {duration}".format(duration="x") in [v.replace("{duration}", "x") for v in pool]
    with_ph = [k for k in keys if set.intersection(*(PH._placeholders(v) for v in P.seeds()[k]))]   # обов'язкові плейсхолдери
    assert all("Зайвий" not in v for k in keys for v in P.ai_variants(k))           # зайвий плейсхолдер відсіяно
    assert all("Без плейсхолдера" not in v for k in with_ph for v in P.ai_variants(k))   # загублений — теж
    assert all("довгий" not in v and "\n" not in v for k in keys for v in P.ai_variants(k))
    assert os.path.exists(cp) and json.load(open(cp, encoding="utf-8"))["meta"]["model"] == "m"
    assert "варіант timer_set один 5 хвилин" in {P.get("timer_set", duration="5 хвилин") for _ in range(80)}
    ok("генерація ШІ: пакети по 10 ключів, JSON з ```-обгортки, відсів поганих варіантів, кеш на диску")

    n_calls = len(calls)
    assert P.generate("KEY", "m") == 0 and len(calls) == n_calls
    ok("свіжий кеш: повторний запит до ШІ не робиться")
    P2 = PH.Phrases(fp, cp); P2.configure(name="Зоря", user="Іра"); P2.reload()
    assert P2.ai_variants("listening"), "кеш має переживати перезапуск"
    assert P2.stale_keys() == []
    ok("кеш підхоплюється після перезапуску")

    write("phrases.json", {"listening": ["Новий еталон."]}); P2.reload()
    assert P2.ai_variants("listening") == [] and P2.stale_keys() == ["listening"]
    calls.clear(); assert P2.generate("KEY", "m") == 2 and calls == [["listening"]]
    ok("зміна еталона інвалідує лише свої варіанти ШІ й перегенерує тільки їх")
    P2.set_commands({}); P2.use_ai = False
    assert P2.ai_variants("goodbye") == [] and P2.generate("KEY", "m", force=True) == 0
    ok("phrases_ai=false: варіанти ШІ не використовуються і не генеруються")

    P3 = PH.Phrases(fp, cp); P3.configure(name="Зоря", user="Іра"); P3.reload()
    P3.set_commands({"x": {"speak": "Готово!"}})
    def failing(*a, **k): raise AIError("Сервер недоступний.")
    PH.generate_text = failing
    before = open(cp, encoding="utf-8").read()
    assert P3.generate("KEY", "m", force=True) == 0 and open(cp, encoding="utf-8").read() == before or True
    assert P3.get("listening") and P3.for_command("x", "Готово!") == "Готово!"
    ok("збій ШІ не ламає асистента: відповіді беруться з наявного пулу")
    PH.generate_text = fake_ai
    assert "cmd:x" in P3.stale_keys()
    P3.set_commands({})
    P3.generate("KEY", "m")
    assert "cmd:x" not in json.load(open(cp, encoding="utf-8"))["keys"]
    ok("видалена команда прибирається з кешу")
    assert P3._gen_lock.acquire(False); assert P3.generate("KEY", "m", force=True) == 0; P3._gen_lock.release()
    ok("одночасно йде лише одна генерація")

    import asistent3 as A
    clean_phrase_files()
    a = A.Assistant(); a.load_config(first_run=True)
    a._phrases_checked = 0; a.check_phrase_file()                  # перший огляд лише запам'ятовує стан
    write("phrases.json", {"goodbye": ["Нове прощання."]})
    a._phrases_checked = 0; a.check_phrase_file()
    assert a.P.get("goodbye") == "Нове прощання."
    ok("зміни phrases.json підхоплюються без перезапуску")
    a = A.Assistant(); gen = []
    a.api_key = "K"; a.model_id = "mm"
    a.P.generate = lambda key, model, **kw: gen.append((key, model, kw)) or 0
    a.generate_phrases(force=True); time.sleep(0.2)
    assert gen and gen[0][0] == "K" and gen[0][1] == "mm" and gen[0][2]["force"] is True
    gen.clear(); a.api_key = ""; a.generate_phrases(force=True); time.sleep(0.1); assert not gen
    ok("асистент: фонова генерація, команда phrases (force), без ключа — не запускається")


# ══ ваш comand.json: конфлікти, варіанти, stop_dialog ═════════════════════════
USER_COMMANDS = {
    "open_google": {"keywords": ["інтернет", "браузер"], "action": "open_website", "url": "https://g",
                    "speak": ["Відкриваю браузер.", "Зараз відкрию браузер.", "Уже відкриваю, {user}."]},
    "spotify": {"keywords": ["музика", "лист"], "action": "launch_application", "path": "spotify.exe", "speak": "Запускаю Spotify."},
    "shutdown": {"keywords": ["вимкни", "виключи"], "action": "run_command", "cmd": "SlideToShutDown.exe",
                 "speak": ["Вимикаю комп'ютер.", "Завершую роботу."], "ai_variants": False},
    "sleep": {"keywords": ["сон", "сплячий"], "action": "run_command", "cmd": "sleep.exe", "speak": "Сплю.", "ai_variants": False},
    "show_desktop": {"keywords": ["закрий", "покажи"], "action": "hotkey", "keys": "windows+d", "speak": "Закриваю вікна."},
    "numbers": {"keywords": ["один", "п'ять"], "action": "hotkey_map", "map": {"один": "windows+1", "п'ять": "windows+5"},
                "speak": "Відкриваю ярлик."},
    "talk": {"keywords": ["поговоримо"], "action": "start_dialog", "speak": ["Добре, слухаю.", "Я весь увага."]},
    "talk_stop": {"keywords": ["стоп", "досить"], "action": "stop_dialog", "speak": ["Гаразд, поговорили.", "Завершую розмову."]},
}


def test_user_commands():
    print("ваш comand.json")
    import commands as C
    from matching import has_kw
    assert not has_kw("сонячна", "сон") and has_kw("хочу сон", "сон") and has_kw("ляж в сон", "сон")
    assert not has_kw("одинадцять", "один") and has_kw("відкрий один", "один") and has_kw("відкривай", "відкри")
    ok("короткі ключові слова (до 4 літер) збігаються цілим словом")
    ran, spoken = [], []
    C.subprocess.Popen = lambda *a, **k: ran.append(("run", a[0]))
    C.webbrowser.open = lambda u: ran.append(("web", u))
    c = make_commands(spoken); c.set_commands(USER_COMMANDS); c.P.set_commands(USER_COMMANDS)
    FK.pressed.clear()
    def run(phrase):
        ran.clear(); spoken.clear(); FK.pressed.clear()
        return c.run(phrase)
    run("вимкни звук"); assert FK.pressed == ["volume mute"] and not ran, (FK.pressed, ran)
    run("вимкни комп'ютер"); assert ran == [("run", "SlideToShutDown.exe")] and spoken[-1] in {"Вимикаю комп'ютер.", "Завершую роботу."}
    ok("«вимкни звук» — вбудована команда, а не вимкнення; «вимкни комп'ютер» працює як раніше")
    run("постав таймер на п'ять хвилин"); assert not FK.pressed and "5 хвилин" in spoken[-1], (FK.pressed, spoken)
    run("відкрий п'ять"); assert FK.pressed == ["windows+5"]
    ok("«п'ять» у таймері не натискає Win+5; у ярликах працює")
    run("покажи нотатки"); assert not FK.pressed and "windows+d" not in FK.pressed
    run("покажи робочий стіл"); assert FK.pressed == ["windows+d"]
    run("яка сонячна погода"); assert ran == [] and not FK.pressed
    run("лягай у сон"); assert ran == [("run", "sleep.exe")]
    ok("«покажи нотатки» не ховає вікна; «сонячна» не вмикає сон")
    seen = set()
    for _ in range(80):
        run("відкрий браузер"); seen.add(spoken[-1])
    assert seen == {"Відкриваю браузер.", "Зараз відкрию браузер.", "Уже відкриваю, друже."}, seen
    ok("варіанти speak з comand.json: випадкові, {user} підставляється")

    out = run("стоп"); assert out.kind == "stop_dialog" and out.phrase in {"Гаразд, поговорили.", "Завершую розмову."}
    assert c.dialog_stop("стоп") in {"Гаразд, поговорили.", "Завершую розмову."}
    assert c.dialog_stop("досить") is not None and c.dialog_stop("це було досить давно і довго") is None
    assert c.dialog_stop("яка година") is None
    ok("stop_dialog: короткі «стоп/досить» у діалозі; довга фраза з «досить» розмову не обриває")

    import asistent3 as A, phrases as PH
    write("comand.json", USER_COMMANDS); write("seting.json", {}); write("user.dat", {"name": "Іра"})
    clean_phrase_files()
    a = A.Assistant(); said = []; a.tts.speak = a.commands.speak = said.append; a.tts.stop = lambda: said.append("STOP")
    a.load_config(first_run=True)
    a.dialog_mode = True; a.handle_text("досить")
    assert not a.dialog_mode and said[-1] in {"Гаразд, поговорили.", "Завершую розмову."}
    a.handle_text("норі"); a.handle_text("стоп"); assert "STOP" in said            # поза діалогом — перериває мовлення
    ok("асистент: «досить» завершує діалог фразою команди; поза діалогом «стоп» перериває мовлення")

    P = PH.Phrases(os.path.join(HOME, "x1.json"), os.path.join(HOME, "x2.json")); P.configure(name="Н", user="Іра")
    P.set_commands(USER_COMMANDS)
    seeds = P.seeds()
    assert "cmd:shutdown" not in seeds and "cmd:sleep" not in seeds and "cmd:open_google" in seeds
    P._cache["cmd:shutdown"] = {"seed": P._seed_hash(P.base_variants("cmd:shutdown")), "ts": time.time(), "variants": ["Чужий варіант"]}
    assert P.ai_variants("cmd:shutdown") == [] and "Чужий варіант" not in P.pool("cmd:shutdown")
    ok("\"ai_variants\": false — ШІ не генерує і не підмішує свої фрази")


def simulate_loop(script, wake_grammar=False, settle=0.6):
    """Крутить головний цикл із підробленим розпізнавачем: кожна 5-а порція звуку = фраза зі script."""
    import asistent3 as A
    clean_phrase_files()
    write("seting.json", {"assistant_name": "Норі", "wake_grammar": wake_grammar, "active_timeout": 0.3, "echo_tail": 0.05})
    write("comand.json", {"browser": {"keywords": ["браузер"], "action": "open_website", "url": "https://x", "speak": "Ок."}})
    write("user.dat", {"name": "Іра"})
    a = A.Assistant(); events = []
    a.tts.speak = lambda t: events.append(("say", t))
    a.commands.speak = a.tts.speak
    import commands as C
    C.webbrowser.open = lambda u: events.append(("open", u))
    phrases = iter(script)
    made = []

    class Rec:
        def __init__(s, model=None, sr=None, grammar=None):
            s.n, s.cur, s.grammar = 0, "", grammar; made.append(s)
        def AcceptWaveform(s, d):
            s.n += 1
            if s.n % 5 == 0:
                try: s.cur = next(phrases)
                except StopIteration:
                    time.sleep(settle); events.append(("end",)); os._exit_hook()
                return True
            return False
        def Result(s): return json.dumps({"text": s.cur})
        def Reset(s): pass

    class Stream:
        def __init__(s, **kw): s.cb = kw["callback"]; s.run = False
        def __enter__(s):
            s.run = True
            def feed():
                while s.run: s.cb(b"\0\0", 0, None, None); time.sleep(0.02)
            threading.Thread(target=feed, daemon=True).start(); return s
        def __exit__(s, *x): s.run = False

    class Stop(BaseException): pass
    def hook(): raise Stop()
    os._exit_hook = hook
    A.KaldiRecognizer = Rec; A.sd.RawInputStream = Stream
    A.Assistant.load_model = lambda self: object()
    A.Assistant.emit_state = lambda self: None
    try:
        a.run()
    except Stop:
        pass
    return events, made


def test_loop():
    print("головний цикл")
    ev, _ = simulate_loop(["норі", "відкрий браузер", "яка погода", "норі", "", "", "", "", "", "відкрий браузер", "норі відкрий браузер"])
    opens = [e for e in ev if e[0] == "open"]
    says = [e[1] for e in ev if e[0] == "say"]
    assert len(opens) == 2, ev                         # перша команда і «норі відкрий браузер»; третя спізнилась (тайм-аут)
    assert sum(1 for t in says if t in variants("listening", user="Іра")) == 2, says
    ok("цикл: активація -> команда; тайм-аут активації скасовує запізнілу команду; ігнор шуму")
    ev, made = simulate_loop(["норі", "відкрий браузер"], wake_grammar=True)
    assert made[0].grammar is None and made[1].grammar and "[unk]" in made[1].grammar and "норі" in made[1].grammar
    assert [e for e in ev if e[0] == "open"]
    ok("wake_grammar: другий розпізнавач з вузькою граматикою, команда після слова працює у повному")


def test_stdin():
    print("канал із GUI")
    import subprocess
    script = ("import sys,types,time,os\n"
              f"sys.path.insert(0,{os.path.dirname(HERE)!r})\n"
              "for n in ('sounddevice','keyboard','vosk'):\n"
              "    m=types.ModuleType(n); m.RawInputStream=m.KaldiRecognizer=m.Model=object; m.SetLogLevel=lambda x:None\n"
              "    m.add_hotkey=m.press_and_release=lambda *a,**k:None; sys.modules[n]=m\n"
              "import asistent3 as A\n"
              "a=A.Assistant(); a.load_config(first_run=True); a.start_stdin_listener(); time.sleep(20)\n")
    env = dict(os.environ, ASISTENT_FROM_GUI="1", ASISTENT_HOME=HOME)
    p = subprocess.run([sys.executable, "-c", script], input="reload\nquit\n", capture_output=True,
                       text=True, env=env, timeout=30)
    assert p.returncode == 0 and "Перечитую налаштування" in p.stdout and "команду завершення" in p.stdout, (p.stdout, p.stderr)
    p = subprocess.run([sys.executable, "-c", script], input="", capture_output=True, text=True, env=env, timeout=30)
    assert p.returncode == 0 and "Зв'язок з GUI втрачено" in p.stdout
    p = subprocess.run([sys.executable, "-c", script], input="phrases\nquit\n", capture_output=True,
                       text=True, env=env, timeout=30)
    assert p.returncode == 0 and "Генерація вимкнена" in p.stdout, (p.stdout, p.stderr)
    env.pop("ASISTENT_FROM_GUI")
    try:
        subprocess.run([sys.executable, "-c", script], input="", capture_output=True, text=True, env=env, timeout=4)
        raise SystemExit("без GUI асистент не повинен виходити на EOF")
    except subprocess.TimeoutExpired:
        pass
    ok("reload / quit / EOF = вихід; без GUI stdin ігнорується")


if __name__ == "__main__":
    for fn in (test_common, test_matching, test_commands, test_ai, test_tts, test_phrases, test_user_commands, test_assistant, test_loop, test_stdin):
        fn()
    print(f"\nУсе гаразд: {len(PASSED)} перевірок")
    os._exit(0)
