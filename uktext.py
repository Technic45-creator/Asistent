"""Українські числа, тривалості, відмінювання та розбиття тексту на речення для озвучення."""
import re

_NUMBERS = {
    "нуль": 0, "один": 1, "одна": 1, "одну": 1, "одне": 1, "два": 2, "дві": 2, "три": 3,
    "чотири": 4, "пять": 5, "шість": 6, "сім": 7, "вісім": 8, "девять": 9, "десять": 10,
    "одинадцять": 11, "дванадцять": 12, "тринадцять": 13, "чотирнадцять": 14,
    "пятнадцять": 15, "шістнадцять": 16, "сімнадцять": 17, "вісімнадцять": 18,
    "девятнадцять": 19, "двадцять": 20, "тридцять": 30, "сорок": 40, "пятдесят": 50,
    "шістдесят": 60, "сімдесят": 70, "вісімдесят": 80, "девяносто": 90, "сто": 100,
    "двісті": 200, "триста": 300, "чотириста": 400, "пятсот": 500, "шістсот": 600,
    "сімсот": 700, "вісімсот": 800, "девятсот": 900, "півтори": 1.5,
}
_APOS = "'’ʼ`"


def _norm(token):
    t = token.lower()
    for a in _APOS:
        t = t.replace(a, "")
    return t


def _tokens(text):
    return [_norm(t) for t in re.findall(r"[\w'’ʼ`]+", text.lower())]


def _number_of(tok):
    if tok.isdigit():
        return int(tok)
    return _NUMBERS.get(tok)


def _unit_seconds(tok):
    if tok.startswith("секунд"):
        return 1
    if tok.startswith("хвилин"):
        return 60
    if tok.startswith("годин"):
        return 3600
    return None


def parse_duration(text):
    """'на двадцять п'ять хвилин' -> 1500; 'на годину' -> 3600; 'на півгодини' -> 1800; нічого -> None."""
    total, cur, found = 0.0, None, False
    for tok in _tokens(text):
        if tok == "півгодини":
            total, found, cur = total + 1800, True, None
        elif tok == "півхвилини":
            total, found, cur = total + 30, True, None
        elif (n := _number_of(tok)) is not None:
            cur = (cur or 0) + n
        elif (mult := _unit_seconds(tok)) is not None:
            total += (cur if cur is not None else 1) * mult
            found, cur = True, None
        else:
            cur = None
    return int(total) if found else None


def parse_number(text):
    """Перше число у фразі (цифрами або словами) або None."""
    cur = None
    for tok in _tokens(text):
        n = _number_of(tok)
        if n is not None:
            cur = (cur or 0) + n
        elif cur is not None:
            break
    return int(cur) if cur is not None else None


def plural(n, one, few, many):
    n = abs(int(n))
    if n % 100 in range(11, 15):
        return many
    if n % 10 == 1:
        return one
    if n % 10 in (2, 3, 4):
        return few
    return many


def format_duration(seconds):
    seconds = int(seconds)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    parts = []
    if h:
        parts.append(f"{h} {plural(h, 'годину', 'години', 'годин')}")
    if m:
        parts.append(f"{m} {plural(m, 'хвилину', 'хвилини', 'хвилин')}")
    if s or not parts:
        parts.append(f"{s} {plural(s, 'секунду', 'секунди', 'секунд')}")
    return " ".join(parts)


MONTHS = ["січня", "лютого", "березня", "квітня", "травня", "червня",
          "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"]
WEEKDAYS = ["понеділок", "вівторок", "середа", "четвер", "пʼятниця", "субота", "неділя"]


# ─── Текст для озвучення ──────────────────────────────────────────────────────
_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+|\n+")


def clean_for_speech(text):
    """Прибирає markdown-символи, які синтезатор прочитав би вголос."""
    text = re.sub(r"[*_`#>]+", "", text or "")
    return re.sub(r"[ \t]+", " ", text).strip()


def pop_sentences(buf):
    """Відрізає від буфера завершені речення: ([речення...], неповний_хвіст)."""
    parts = _SENT_SPLIT.split(buf)
    return [p for p in parts[:-1] if p.strip()], parts[-1]


def split_sentences(text):
    done, tail = pop_sentences(text)
    return done + ([tail] if tail.strip() else [])
