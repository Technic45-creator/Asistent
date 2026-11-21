"""Пошук команд за ключовими словами (спільний для асистента і GUI)."""
import re


def has_kw(text, kw):
    """Збіг з початку слова: 'відкрий' знайдеться у 'відкривай', але 'норі' — не всередині слова.
    Короткі слова (до 4 літер) мають збігатися цілком: 'сон' не знайдеться у 'сонячна', 'один' — в 'одинадцять'."""
    kw = str(kw).strip().lower()
    if not kw:
        return False
    tail = r"(?!\w)" if len(kw) <= 4 else ""
    return re.search(r"(?<!\w)" + re.escape(kw) + tail, text) is not None


def kw_list(info):
    kws = info.get("keywords", []) if isinstance(info, dict) else []
    return [kws] if isinstance(kws, str) else [str(k) for k in kws]


def is_enabled(info):
    return isinstance(info, dict) and not info.get("disabled")


def kw_score(info, text):
    """Довжина найдовшого ключового слова команди, що збіглося з фразою (0 — не збіглося)."""
    return max((len(k.strip()) for k in kw_list(info) if has_kw(text, k)), default=0)


def match_commands(commands, text):
    """Команди, що підходять до фрази: від найточнішої (найдовше ключове слово).
    Вимкнені (disabled) пропускаються."""
    scored = []
    for key, info in commands.items():
        if not is_enabled(info):
            continue
        score = kw_score(info, text)
        if score:
            scored.append((score, key, info))
    scored.sort(key=lambda t: -t[0])      # сортування стабільне: при рівності — порядок у файлі
    return [(key, info) for _, key, info in scored]


def find_overlaps(commands, key, keywords):
    """Ключові слова нової/зміненої команди, що перетинаються зі словами інших команд.
    Повертає [(своє_слово, ключ_іншої_команди, її_слово)]."""
    found = []
    for other_key, info in commands.items():
        if other_key == key or not is_enabled(info):
            continue
        for mine in keywords:
            for theirs in kw_list(info):
                if mine.strip().lower() == theirs.strip().lower():
                    found.append((mine, other_key, theirs))
                elif has_kw(mine.lower(), theirs) or has_kw(theirs.lower(), mine):
                    found.append((mine, other_key, theirs))
    return found
