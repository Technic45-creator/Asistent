"""Виконання команд: користувацькі (comand.json) і вбудовані (час, таймер, гучність, нотатки, пошук).
Ті самі дії доступні AI як функції (function calling)."""
import logging
import os
import re
import subprocess
import threading
import webbrowser
from collections import namedtuple
from datetime import datetime
from urllib.parse import quote_plus

from matching import has_kw, is_enabled, kw_list, kw_score, match_commands
from uktext import MONTHS, WEEKDAYS, format_duration, parse_duration, parse_number

log = logging.getLogger("asistent")

CREATE_NO_WINDOW = 0x08000000
# kind: "done" — виконано (або повідомлено про проблему); "exit" / "dialog" — потрібна реакція
# асистента; "none" — жодна команда не підійшла.
Outcome = namedtuple("Outcome", "kind phrase")


def _W(alternatives):
    """Регулярний вираз, що збігається лише з початку слова ('год' не знайдеться у 'сьогодні')."""
    return re.compile(r"(?<!\w)(?:" + alternatives + ")")


def text_after(text, keywords):
    """Текст після найдовшого знайденого ключового слова."""
    best = None
    for kw in keywords:
        m = re.search(r"(?<!\w)" + re.escape(kw.lower()) + r"\w*", text)
        if m and (best is None or len(kw) > best[0]):
            best = (len(kw), m.end())
    return text[best[1]:].strip(" ,.") if best else ""


class Commands:
    def __init__(self, speak, cfg, phrases, notes_path, keyboard_mod=None):
        """speak(text); cfg(key, default); phrases — об'єкт Phrases (варіанти відповідей);
        notes_path() -> шлях; keyboard_mod — модуль `keyboard` (передається ззовні для тестів)."""
        self.speak = speak
        self.cfg = cfg
        self.P = phrases
        self.notes_path = notes_path
        self.kb = keyboard_mod
        self.commands = {}
        self._timers = []
        self._timers_lock = threading.Lock()
        self.builtins = self._make_builtins()

    def set_commands(self, commands):
        self.commands = commands

    # ── користувацькі дії ────────────────────────────────────────────────────
    def _keyboard(self):
        if self.kb is None:
            import keyboard
            self.kb = keyboard
        return self.kb

    @staticmethod
    def _launch_app(path):
        try:
            os.startfile(path)                  # як у Провіднику (Windows)
        except (OSError, AttributeError):
            subprocess.Popen(path, shell=True)

    @staticmethod
    def _run_command(info):
        # "args": ["prog", "arg"] запускає без shell (безпечніше); "cmd": "..." — через shell
        args = info.get("args")
        flags = CREATE_NO_WINDOW if os.name == "nt" else 0
        if isinstance(args, list) and args:
            subprocess.Popen([str(a) for a in args], creationflags=flags)
        else:
            subprocess.Popen(info.get("cmd", ""), shell=True, creationflags=flags)

    def _do_action(self, info, text):
        """True — виконано, False — помилка, None — ця команда не підійшла (шукаємо далі)."""
        action = info.get("action", "")
        try:
            if action == "open_website":
                webbrowser.open(info.get("url", "https://www.google.com"))
            elif action == "launch_application":
                if not info.get("path"):
                    raise ValueError("не вказано path")
                self._launch_app(info["path"])
            elif action == "hotkey":
                self._keyboard().press_and_release(info.get("keys", ""))
            elif action == "hotkey_map":
                hk = next((h for w, h in info.get("map", {}).items() if has_kw(text, w)), None)
                if not hk:
                    return None
                self._keyboard().press_and_release(hk)
            elif action == "run_command":
                self._run_command(info)
            else:
                return None
            return True
        except Exception as e:
            log.info(f"[ПОМИЛКА] {e}")
            self.speak(self.P.get("error"))
            return False

    # ── вбудовані команди ────────────────────────────────────────────────────
    def _make_builtins(self):
        B = lambda name, kws, parse, run, desc, params=None, hint="": dict(
            name=name, keywords=kws, parse=parse, run=run, hint=hint,
            decl={"name": name, "description": desc, **({"parameters": params} if params else {})})
        obj = lambda props, req=None: {"type": "OBJECT", "properties": props, **({"required": req} if req else {})}
        s, i = {"type": "STRING"}, {"type": "INTEGER"}
        return [
            B("get_datetime",
              ["яка година", "котра година", "скільки часу", "який час", "яка дата", "яке число",
               "який сьогодні день", "яка сьогодні дата", "яке сьогодні число"],
              lambda t: {"part": "time" if _W(r"год|час").search(t) else "date"},
              self._b_datetime, "Поточні час і/або дата користувача.",
              obj({"part": {"type": "STRING", "enum": ["time", "date", "both"]}})),
            B("set_timer", ["постав таймер", "поставити таймер", "запусти таймер", "таймер на", "нагадай через"],
              lambda t: ({"seconds": d} if (d := parse_duration(t)) else None),
              self._b_timer, "Запустити таймер; по завершенні асистент голосом повідомить.",
              obj({"seconds": i, "label": s}, ["seconds"]),
              hint="hint_timer"),
            B("cancel_timers", ["скасуй таймер", "зупини таймер", "вимкни таймер", "скасувати таймер"],
              lambda t: {}, self._b_cancel_timers, "Скасувати всі запущені таймери."),
            B("set_volume",
              ["гучність", "голосніше", "тихіше", "вимкни звук", "без звуку", "заглуш звук"],
              self._parse_volume, self._b_volume,
              "Змінити системну гучність: up/down — на 10%, mute — вимкнути звук, set — виставити percent.",
              obj({"action": {"type": "STRING", "enum": ["up", "down", "mute", "set"]}, "percent": i}, ["action"]),
              hint="hint_volume"),
            B("save_note", ["запиши нотатку", "створи нотатку", "додай нотатку", "нова нотатка", "запиши нотатка"],
              lambda t: ({"text": x} if (x := text_after(t, ["запиши нотатку", "створи нотатку", "додай нотатку",
                                                              "нова нотатка", "запиши нотатка"])) else None),
              self._b_note, "Записати нотатку користувача у файл.", obj({"text": s}, ["text"]),
              hint="hint_note"),
            B("read_notes", ["прочитай нотатки", "мої нотатки", "покажи нотатки", "останні нотатки"],
              lambda t: {}, self._b_read_notes, "Прочитати кілька останніх нотаток."),
            B("web_search",
              ["знайди в гуглі", "знайди в інтернеті", "пошукай в інтернеті", "пошукай в гуглі", "загугли", "погугли"],
              lambda t: ({"query": x} if (x := text_after(t, ["знайди в гуглі", "знайди в інтернеті",
                         "пошукай в інтернеті", "пошукай в гуглі", "загугли", "погугли"])) else None),
              self._b_search, "Відкрити у браузері пошук Google за запитом.", obj({"query": s}, ["query"]),
              hint="hint_search"),
        ]

    def _b_datetime(self, a):
        now = datetime.now()
        t = f"{now.hour}:{now.minute:02d}"
        d = f"{now.day} {MONTHS[now.month - 1]}, {WEEKDAYS[now.weekday()]}"
        part = a.get("part", "both")
        if part == "time":
            return self.P.get("time_now", time=t)
        if part == "date":
            return self.P.get("date_today", date=d)
        return self.P.get("datetime_both", time=t, date=d)

    def _b_timer(self, a):
        seconds = max(1, int(a["seconds"]))
        label = str(a.get("label") or "").strip()
        timer = threading.Timer(seconds, self._timer_done, args=(seconds, label))
        timer.daemon = True
        with self._timers_lock:
            self._timers.append(timer)
        timer.start()
        return self.P.get("timer_set", duration=format_duration(seconds))

    def _timer_done(self, seconds, label):
        self.speak(self.P.get("timer_done_label", label=label) if label
                   else self.P.get("timer_done", duration=format_duration(seconds)))

    def _b_cancel_timers(self, a):
        with self._timers_lock:
            active = [t for t in self._timers if t.is_alive()]
            for t in active:
                t.cancel()
            self._timers.clear()
        return self.P.get("timers_cancelled" if active else "timers_none")

    @staticmethod
    def _parse_volume(t):
        n = parse_number(t)
        if _W(r"вимкни звук|без звуку|заглуш").search(t):
            return {"action": "mute"}
        if n is not None and _W(r"встанови|постав|зроби|на\s").search(t):
            return {"action": "set", "percent": n}
        if _W(r"вище|більше|голосніше|збільш|підвищ|додай|підніми").search(t):
            return {"action": "up"}
        if _W(r"нижче|менше|тихіше|зменш|зниж|убав|понизь").search(t):
            return {"action": "down"}
        return None

    def _b_volume(self, a):
        kb = self._keyboard()
        action = a.get("action")
        if action == "mute":
            kb.press_and_release("volume mute")
            return self.P.get("volume_mute")
        if action in ("up", "down"):
            for _ in range(5):                       # одне натискання = 2%
                kb.press_and_release(f"volume {action}")
            return self.P.get("volume_up" if action == "up" else "volume_down")
        if action == "set":
            percent = max(0, min(100, int(a.get("percent", 50))))
            for _ in range(50):
                kb.press_and_release("volume down")  # до нуля
            for _ in range(percent // 2):
                kb.press_and_release("volume up")
            return self.P.get("volume_set", percent=percent)
        raise ValueError("невідома дія з гучністю")

    def _b_note(self, a):
        text = str(a.get("text", "")).strip()
        if not text:
            raise ValueError("порожня нотатка")
        with open(self.notes_path(), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M}  {text}\n")
        return self.P.get("note_saved")

    def _b_read_notes(self, a):
        try:
            with open(self.notes_path(), encoding="utf-8") as f:
                lines = [ln.strip() for ln in f if ln.strip()]
        except OSError:
            return self.P.get("notes_none")
        if not lines:
            return self.P.get("notes_none")
        return self.P.get("notes_read", notes=" ".join(ln[17:].strip() if len(ln) > 17 else ln for ln in lines[-3:]))

    def _b_search(self, a):
        query = str(a.get("query", "")).strip()
        if not query:
            raise ValueError("порожній запит")
        webbrowser.open("https://www.google.com/search?q=" + quote_plus(query))
        return self.P.get("searching", query=query)

    def _match_builtin(self, text):
        """(довжина_ключового_слова, вбудована_команда) або None."""
        best = None
        for b in self.builtins:
            lens = [len(k) for k in b["keywords"] if has_kw(text, k)]
            if lens and (best is None or max(lens) > best[0]):
                best = (max(lens), b)
        return best

    # ── головний вхід ────────────────────────────────────────────────────────
    def has_match(self, text):
        if match_commands(self.commands, text):
            return True
        return bool(self.cfg("builtins", True)) and self._match_builtin(text) is not None

    def _run_builtin(self, b, text):
        args = b["parse"](text)
        if args is None:
            self.speak(self.P.get(b["hint"] or "error"))
            return Outcome("done", "")
        try:
            reply = b["run"](args)
            log.info(f"[КОМАНДА] {b['name']}")
            self.speak(reply)
        except Exception as e:
            log.info(f"[ПОМИЛКА] {b['name']}: {e}")
            self.speak(self.P.get("error"))
        return Outcome("done", "")

    def run(self, text):
        user_matches = match_commands(self.commands, text)
        builtin = self._match_builtin(text) if self.cfg("builtins", True) else None
        # Перемагає точніший збіг (довше ключове слово): «вимкни звук» — вбудована команда, а не ваше «вимкни».
        # При однаковій довжині ваші команди мають пріоритет.
        if builtin and (not user_matches or builtin[0] > kw_score(user_matches[0][1], text)):
            return self._run_builtin(builtin[1], text)

        for key, info in user_matches:
            action = info.get("action", "")
            if action == "stop_dialog":
                return Outcome("stop_dialog", self.P.for_command(key, info.get("speak")))
            if action == "exit":
                return Outcome("exit", self.P.for_command(key, info.get("speak")))
            if action == "start_dialog":
                return Outcome("dialog", self.P.for_command(key, info.get("speak")))
            ok = self._do_action(info, text)
            if ok is None:
                continue
            log.info(f"[КОМАНДА] {key} -> {action}")
            if ok:
                self.speak(self.P.for_command(key, info.get("speak")))
            return Outcome("done", "")

        if builtin:
            return self._run_builtin(builtin[1], text)
        return Outcome("none", "")

    def dialog_stop(self, text):
        """Під час діалогу: чи це команда «стоп діалог» (action=stop_dialog). Повертає її фразу (можливо, '')
        або None. Лише для коротких реплік (до 3 слів), щоб «це було досить давно» не обривало розмову."""
        if len(text.split()) > 3:
            return None
        for key, info in match_commands(self.commands, text):
            if info.get("action") == "stop_dialog":
                return self.P.for_command(key, info.get("speak"))
        return None

    # ── функції для AI ───────────────────────────────────────────────────────
    def _user_command_ids(self):
        return [k for k, info in self.commands.items()
                if is_enabled(info) and info.get("action") not in ("exit", "start_dialog", None, "")]

    def tool_declarations(self):
        decls = []
        ids = self._user_command_ids()
        if ids:
            listing = "; ".join(f"{k} ({', '.join(kw_list(self.commands[k])[:3])})" for k in ids)
            decls.append({
                "name": "execute_command",
                "description": "Виконати команду користувача на комп'ютері (відкрити сайт чи програму, "
                               "натиснути клавіші). Доступні команди: " + listing,
                "parameters": {"type": "OBJECT", "properties": {
                    "command": {"type": "STRING", "enum": ids},
                    "argument": {"type": "STRING", "description": "Уточнення, якщо команда його потребує"},
                }, "required": ["command"]},
            })
        if self.cfg("builtins", True):
            decls += [b["decl"] for b in self.builtins]
        return decls

    def call_tool(self, name, args):
        args = args or {}
        if name == "execute_command":
            info = self.commands.get(args.get("command"))
            if not is_enabled(info):
                return "Такої команди немає або вона вимкнена."
            ok = self._do_action(info, str(args.get("argument", "")).lower())
            if ok:
                return "Виконано."
            return "Команда не підійшла." if ok is None else "Не вдалося виконати."
        for b in self.builtins:
            if b["name"] == name and self.cfg("builtins", True):
                return b["run"](args)
        return "Невідома функція."

    def cancel_timers(self):
        self._b_cancel_timers({})
