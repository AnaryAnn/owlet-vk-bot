import os
import re
import random
import threading
import math
import time
import json
import hmac
import hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from uuid import uuid4
import requests
from flask import Flask, request, Response, jsonify
from quest_engine import tick as quest_tick
from quest_milestones import send_milestones
from report_comments import append_overplan_comment

app = Flask(__name__)
from website_api import website_api
app.register_blueprint(website_api)

def report_diag(report_id, stage, started=None, **extra):
    now = time.monotonic()
    elapsed = None if started is None else round(now - started, 3)
    details = " ".join(f"{k}={v!r}" for k, v in extra.items())
    print(
        f"REPORT DIAG report_id={report_id} stage={stage} "
        f"elapsed={elapsed}s {details}".rstrip(),
        flush=True,
    )


VK_TOKEN = os.environ.get("VK_TOKEN", "").strip()
VK_GROUP_ID = os.environ.get("VK_GROUP_ID", "").strip()
VK_CONFIRMATION_CODE = os.environ.get("VK_CONFIRMATION_CODE", "").strip()
# Сбор для ежедневного выпуска. Явное SAVE_CHAT_MESSAGES=false отключает его.
SAVE_CHAT_MESSAGES = os.environ.get("SAVE_CHAT_MESSAGES", "true").strip().lower() in ("1", "true", "yes", "on")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
SYCHEVESTNIK_PEER_ID = 2000000002
STATS_PEER_ID = 2000000002
MORNING_PHOTO = "photo-241605282_457239021"
EVENING_PHOTO = os.environ.get("EVENING_PHOTO", "photo-241605282_457239023").strip()
SCHEDULE_SECRET = os.environ.get("SCHEDULE_SECRET", "").strip()

PAIR_RE = re.compile(r"^\s*(\d+)\s*,\s*(\d+)\s*$")
PLAIN_REPORT_RE = re.compile(r"^\s*робосычик\s+(\d+)\s*,\s*(\d+)\s*$", re.IGNORECASE)

_COMMAND_EVENT_LOCK = threading.Lock()
_COMMAND_EVENT_IDS = {}
_COMMAND_EVENT_TTL = 300

def command_event_once(event_id):
    """Возвращает True только для первой обработки event_id в этом процессе."""
    if not event_id:
        return True

    now = time.time()
    key = str(event_id)

    with _COMMAND_EVENT_LOCK:
        expired = [
            eid for eid, ts in _COMMAND_EVENT_IDS.items()
            if now - ts > _COMMAND_EVENT_TTL
        ]
        for eid in expired:
            _COMMAND_EVENT_IDS.pop(eid, None)

        if key in _COMMAND_EVENT_IDS:
            return False

        _COMMAND_EVENT_IDS[key] = now
        return True


def google_post(payload):
    from native_storage import stats_post
    return stats_post(payload)


def google_get(params=None):
    from native_storage import stats_get
    return stats_get(params)


def _num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _fmt_num(value):
    n = _num(value)
    return str(int(n)) if n.is_integer() else f"{n:g}"


def build_weekly_stats():
    """Собирает статистику текущей недели через Google Sheets API."""
    stats = google_get({"action": "stats"})
    if not stats.get("success"):
        raise RuntimeError(stats.get("error", "Не удалось получить статистику"))

    records = stats.get("currentRecords") or []

    # Новая версия API отдаёт участников вместе со статистикой, поэтому обычно
    # Робосычик делает только один запрос к Google. Старый API поддерживается
    # через запасной запрос participants.
    participants = stats.get("participants") or []
    participants_data = {}

    if not participants:
        try:
            participants_data = google_get({"action": "participants"})
            if participants_data.get("success"):
                participants = participants_data.get("participants") or []
        except Exception as exc:
            print("PARTICIPANTS FALLBACK ERROR:", repr(exc), flush=True)

    # Если API отдаёт статусы, считаем персональные показатели только по активным.
    active_names = set()
    if participants:
        for p in participants:
            name = str(p.get("name") or "").strip()
            status = str(p.get("status") or "активен").strip().lower()
            if name and status != "выбыл":
                active_names.add(name)

    if active_names:
        active_records = [
            r for r in records
            if str(r.get("name") or "").strip() in active_names
        ]
        active_count = len(active_names)
    else:
        active_records = records
        active_count = int(
            stats.get("activePeopleCount")
            or participants_data.get("activePeopleCount")
            or stats.get("peopleCount")
            or participants_data.get("peopleCount")
            or 0
        )

    # Отчёт считаем сданным, если у записи есть отметка времени обновления.
    submitted = [
        r for r in active_records
        if str(r.get("updated") or "").strip()
    ]

    completed = sum(
        1 for r in submitted
        if _num(r.get("fact")) >= _num(r.get("plan"))
    )
    exceeded = sum(
        1 for r in submitted
        if _num(r.get("fact")) > _num(r.get("plan"))
    )

    # Командные план и факт считаем по всем записям текущей недели.
    # Если участник выбыл уже после сдачи отчёта, его пойманные мыши не исчезают
    # из командного результата. Статус влияет только на персональные счётчики
    # и на делитель "сколько добавить каждому активному".
    total_plan = sum(_num(r.get("plan")) for r in records)
    total_fact = sum(_num(r.get("fact")) for r in records)

    goal_raw = stats.get("teamGoal")
    has_goal = goal_raw not in (None, "")
    goal = _num(goal_raw) if has_goal else None

    if goal is None:
        goal_percent = None
        add_per_person = None
    elif goal <= 0:
        goal_percent = 100.0 if total_fact >= goal else 0.0
        add_per_person = 0
    else:
        goal_percent = total_fact / goal * 100
        shortage = max(0, goal - total_fact)
        add_per_person = math.ceil(shortage / active_count) if active_count else 0

    week = stats.get("currentWeek") or {}
    start = str(week.get("startDisplay") or week.get("start") or "")
    end = str(week.get("endDisplay") or week.get("end") or "")
    period = f"{start} - {end}" if start or end else "текущая неделя"

    goal_text = _fmt_num(goal) if goal is not None else "не задана"
    percent_text = f"{goal_percent:.1f}%".replace(".0%", "%") if goal_percent is not None else "нет цели"
    add_text = str(add_per_person) if add_per_person is not None else "не считается, пока не задана цель"

    return (
        "📊 Статистика мохноногих сычиков\n"
        f"Неделя: {period}\n\n"
        f"🦉 Активных сычиков: {active_count}\n"
        f"📝 Сообщили план и факт: {len(submitted)}\n"
        f"✅ Выполнили свой план: {completed}\n"
        f"🚀 Перевыполнили план: {exceeded}\n\n"
        f"🐭 План: {_fmt_num(total_plan)}\n"
        f"🐭 Факт: {_fmt_num(total_fact)}\n"
        f"🎯 Цель: {goal_text}\n"
        f"📈 Цель выполнена на: {percent_text}\n"
        f"➕ Нужно добавить каждому активному сычику: {add_text} 🐭"
    )

def digest_post(payload, timeout=12):
    from native_storage import digest_post as native_digest
    return native_digest(payload)


def extract_final_digest(content):
    text = (content or "").strip()
    if not text:
        raise RuntimeError("OpenRouter returned an empty digest")

    marked = re.search(
        r"FINAL_DIGEST_START\s*(.*?)\s*FINAL_DIGEST_END",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if marked:
        text = marked.group(1).strip()
    else:
        # Free models can sometimes expose their scratchpad. If markers were
        # ignored, keep only the final section beginning with our heading.
        positions = [m.start() for m in re.finditer(r"📰\s*(?:\*\*)?Сычевестник", text, re.IGNORECASE)]
        if positions:
            text = text[positions[-1]:].strip()
        else:
            raise RuntimeError("OpenRouter response has no final digest marker")

    # Remove accidental closing marker and common meta-commentary after the digest.
    text = re.sub(r"\s*FINAL_DIGEST_END.*$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    for stop in (
        "\nMake sure",
        "\nLet's ",
        "\nWe need ",
        "\nNow ",
        "\nI used ",
    ):
        pos = text.find(stop)
        if pos != -1:
            text = text[:pos].rstrip()

    # Ничего не обрезаем здесь. Полный текст дайджеста сохраняется целиком.

    return text


def build_local_digest(useful):
    """Резервный выпуск без ИИ: только реальные цитаты из беседы."""
    extracts = []
    seen = set()
    for entry in useful:
        name, separator, raw_text = entry.partition(": ")
        if not separator:
            continue
        message = re.sub(r"\s+", " ", raw_text).strip()
        # Односложные ответы и дубли для подборки не подходят.
        if len(message) < 24:
            continue
        key = message.casefold()
        if key in seen:
            continue
        seen.add(key)
        extracts.append((name, message))

    if not extracts:
        return (
            "📰 Сычевестник\n\n"
            "⚙️ ИИ-редактор временно недоступен. "
            "Для резервной подборки не нашлось достаточно содержательных сообщений. 🤖"
        )

    # Берём сообщения из разных частей переписки, сохраняя исходный порядок.
    recent = extracts[-30:]
    count = min(6, len(recent))
    if count == 1:
        selected = recent
    else:
        indexes = [round(i * (len(recent) - 1) / (count - 1)) for i in range(count)]
        selected = [recent[i] for i in indexes]

    bullets = []
    for name, message in selected:
        if len(message) > 230:
            message = message[:229].rsplit(" ", 1)[0].rstrip() + "…"
        bullets.append(f"• {name}: «{message}»")

    return (
        "📰 Сычевестник\n\n"
        "⚙️ ИИ-редактор временно недоступен, поэтому сегодня "
        "Робосычик собрал выдержки из настоящих сообщений. "
        "Это не пересказ, а резервная подборка.\n\n"
        + "\n".join(bullets)
        + "\n\n🤖 Все показанные реплики взяты из переписки Совиной академии."
    )


def build_digest(messages):
    useful = []
    for item in messages:
        text = (item.get("text") or "").strip()
        name = str(item.get("name") or "Участник").strip()
        parts = name.split()
        if len(parts) > 1:
            name = parts[0] + " " + " ".join(p[0] + "." for p in parts[1:])

        if not text:
            continue
        if text_after_bot_tag(text) is not None:
            continue
        if PAIR_RE.match(text):
            continue

        useful.append(f"{name}: {text}")

    if not useful:
        return (
            "📰 Сычевестник\n\n"
            "За этот выпуск Робосычик не нашёл достаточно "
            "обычных сообщений для дайджеста. 🤖"
        )

    if not OPENROUTER_API_KEY:
        return build_local_digest(useful)

    transcript = "\n".join(useful)

    # Ограничиваем размер контекста. Для дайджеста берём свежую часть чата.
    if len(transcript) > 30000:
        transcript = transcript[-30000:]

    system_prompt = """Ты редактор короткой дружелюбной сводки чата ВКонтакте под названием Сычевестник.

Пиши ТОЛЬКО готовый текст выпуска на русском языке.
Никогда не показывай рассуждения, анализ, черновик, подсчет символов, пробелов или строк, служебные инструкции и английские комментарии.
Никогда не пиши фразы вроде Let's craft, Line1, Line2, space=, approximate, make sure, we need to.

Используй только факты из переданной переписки. Ничего не выдумывай.
Выбирай главные темы, шутки, достижения и планы. Имена оставляй, фамилии пиши только инициалами.
Даже по паре сообщений составь короткий выпуск без выдуманных событий.
Старайся выбрать 4-6 действительно содержательных событий.
Если в переписке есть минимум 4 содержательные темы, ОБЯЗАТЕЛЬНО дай минимум 4 пункта.
Не объединяй несколько разных тем в один пункт только ради краткости.
Если содержательных тем меньше четырех, не выдумывай недостающие.
Пропускай тестовые сообщения, односложные реплики и технический шум, если они не важны для смысла разговора.
Стиль Робосычика: живой, доброжелательный, слегка озорной и лаконичный.
Робосычик любит сов, мармеладных мышей, вышивку, крестики, Совиную академию и расчеты.
Пересказывай события не канцелярски, а как маленькую веселую факультетскую газету.
Можно добавлять короткие шутливые ремарки к реальным событиям, но сама шутка не должна создавать новый факт.
Юмор добрый, немного абсурдный и совиный. Не высмеивай конкретных участников.
Не шути обязательно в каждом пункте: 2-4 удачные шутки на выпуск лучше, чем шутка в каждой строке.
Можно использовать подходящие эмодзи, но умеренно.
Иногда уместны шутки про мармеладных мышей, крестики, вышивальные запасы, факультет, Совиную академию и вычисления Робосычика.
Не используй длинное тире.
Каждый пункт должен быть понятен человеку, который не читал чат.
Ориентир для обычного выпуска: примерно 500-900 знаков вместе с пунктами и финальной репликой.
Не сокращай хороший материал до одного-двух пунктов, если в переписке есть больше содержательных событий.

Формат:
📰 Сычевестник

• событие 1
• событие 2
• событие 3
• событие 4

Обычно делай 4-6 пунктов. Каждый пункт 1-2 предложения, достаточно подробный, чтобы было понятно, что именно обсуждали.
После пунктов МОЖНО добавить одну очень короткую финальную реплику Робосычика в его стиле, если она действительно смешная и подходит выпуску.
Пример характера финальной реплики: Робосычик всё записал. Мыши предупреждены. 🤖🦉
Не копируй пример каждый раз, придумывай разные финальные реплики.
Не повторяй заголовок.
Не добавляй пояснения, анализ, служебные комментарии или метакомментарии."""

    request_body = {
        "model": None,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": "Переписка за период выпуска:\n\n" + transcript,
            },
        ],
        "temperature": 0.8,
        "max_tokens": 2400,
    }

    # Автомаршрутизатор выбирает доступную бесплатную модель; следующие две
    # используются, если ответ оказался некорректным или модель недоступна.
    # При исчерпании суточной квоты OpenRouter ни одна бесплатная модель
    # не поможет: в этом случае ниже сработает локальная подборка цитат.
    models = [
        "openrouter/free",
        "dots-studio/dots-3-note-preview-20260813:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
    ]
    last_error = None

    for attempt, model in enumerate(models, start=1):
        try:
            request_body["model"] = model
            print(
                f"OPENROUTER ATTEMPT {attempt}/{len(models)} model={model}",
                flush=True,
            )

            response = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=request_body,
                timeout=12,
            )

            if response.status_code == 429:
                last_error = RuntimeError(
                    f"{model}: OpenRouter rate limit 429"
                )
                print(
                    f"OPENROUTER RATE LIMITED model={model}, switching model",
                    flush=True,
                )
                continue

            response.raise_for_status()
            data = response.json()

            if data.get("error"):
                error = data["error"]
                raise RuntimeError(error.get("message", str(error)))

            choices = data.get("choices") or []
            if not choices:
                raise RuntimeError("OpenRouter returned no choices")

            message = choices[0].get("message") or {}
            content = (message.get("content") or "").strip()
            finish_reason = choices[0].get("finish_reason")

            # Если модель упёрлась в лимит генерации, не отправляем обрубок.
            # Пробуем следующую модель из списка fallback.
            if finish_reason in ("length", "max_tokens"):
                print(
                    f"OPENROUTER TRUNCATED OUTPUT attempt={attempt} finish_reason={finish_reason}",
                    flush=True,
                )
                raise RuntimeError("OpenRouter truncated digest by token limit")

            if not content:
                safe_debug = {
                    "model": data.get("model"),
                    "provider": data.get("provider"),
                    "finish_reason": choices[0].get("finish_reason"),
                    "message_keys": sorted(message.keys()),
                    "has_reasoning": bool(message.get("reasoning")),
                    "reasoning_length": len(message.get("reasoning") or ""),
                    "usage": data.get("usage"),
                }
                print(
                    f"OPENROUTER EMPTY CONTENT attempt={attempt}: {safe_debug}",
                    flush=True,
                )
                raise RuntimeError("OpenRouter returned an empty digest")

            digest = extract_final_digest(content)

            forbidden_markers = (
                "let's craft",
                "line1",
                "line2",
                "line3",
                "space=",
                "approximate:",
                "make sure",
                "we need to",
            )
            digest_lower = digest.lower()
            if any(marker in digest_lower for marker in forbidden_markers):
                print(
                    f"OPENROUTER REJECTED META OUTPUT attempt={attempt}",
                    flush=True,
                )
                raise RuntimeError(
                    "OpenRouter returned meta/reasoning text instead of a digest"
                )

            if "Сычевестник" not in digest:
                raise RuntimeError(
                    "OpenRouter response does not contain the required digest header"
                )

            print(
                f"OPENROUTER SUCCESS attempt={attempt}: {len(digest)} chars",
                flush=True,
            )
            return digest

        except Exception as e:
            last_error = e
            print(
                f"OPENROUTER ATTEMPT {attempt} FAILED: {type(e).__name__}: {e}",
                flush=True,
            )

            if attempt < len(models):
                print(
                    "OPENROUTER SWITCHING TO FALLBACK MODEL",
                    flush=True,
                )

    # Не теряем запланированный выпуск, даже если у бесплатного OpenRouter
    # закончились запросы или все модели временно недоступны.
    print(
        "OPENROUTER ALL MODELS FAILED; LOCAL DIGEST FALLBACK: "
        + str(last_error or "unknown error"),
        flush=True,
    )
    return build_local_digest(useful)

def split_vk_message(text, limit=3900):
    """Делит длинное сообщение VK на части без потери текста."""
    text = str(text or "")
    if len(text) <= limit:
        return [text]

    parts = []
    rest = text
    while len(rest) > limit:
        chunk = rest[:limit]

        # Сначала стараемся делить между абзацами или пунктами.
        cut = chunk.rfind("\n\n")
        if cut < int(limit * 0.55):
            cut = chunk.rfind("\n")
        if cut < int(limit * 0.55):
            cut = chunk.rfind(" ")
        if cut <= 0:
            cut = limit

        part = rest[:cut].rstrip()
        if part:
            parts.append(part)
        rest = rest[cut:].lstrip()

    if rest:
        parts.append(rest)
    return parts


def vk_send(peer_id, text, attachment=None, keyboard=None, random_id=None):
    chunks = split_vk_message(text)
    last_result = None

    for index, chunk in enumerate(chunks):
        data = {
            "access_token": VK_TOKEN,
            "v": "5.199",
            "peer_id": peer_id,
            "random_id": random.randint(1, 2147483647) if random_id is None else (
                int.from_bytes(hashlib.sha256(f"{random_id}:{index}".encode()).digest()[:4], "big")
                & 0x7fffffff) or 1,
            "message": chunk,
        }
        if keyboard is not None and index == 0:
            data["keyboard"] = json.dumps(keyboard, ensure_ascii=False)
        # Картинку прикрепляем только к первой части выпуска.
        if attachment and index == 0:
            data["attachment"] = attachment

        r = requests.post(
            "https://api.vk.com/method/messages.send",
            data=data,
            timeout=15,
        )
        r.raise_for_status()

        result = r.json()
        if result.get("error"):
            raise RuntimeError(
                "VK messages.send error: " +
                str(result["error"].get("error_msg", result["error"]))
            )
        last_result = result

    return last_result or {"response": None}



def vk_send_reliable(peer_id, text, attachment=None, attempts=6):
    """
    Надёжная отправка коротких служебных ответов Робосычика.
    Один random_id сохраняется между повторами, чтобы при сетевом таймауте
    VK не создал дубликат уже принятого сообщения.
    """
    random_id = random.randint(1, 2147483647)
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            print(
                f"VK CONFIRM ATTEMPT {attempt}/{attempts} "
                f"peer_id={peer_id} random_id={random_id}",
                flush=True,
            )

            data = {
                "access_token": VK_TOKEN,
                "v": "5.199",
                "peer_id": peer_id,
                "random_id": random_id,
                "message": str(text or ""),
            }
            if attachment:
                data["attachment"] = attachment

            r = requests.post(
                "https://api.vk.com/method/messages.send",
                data=data,
                timeout=(5, 12),
            )

            # Для временных HTTP-ошибок пробуем ещё раз.
            if r.status_code == 429 or 500 <= r.status_code < 600:
                raise RuntimeError(
                    f"VK HTTP {r.status_code}: {r.text[:300]}"
                )

            r.raise_for_status()
            result = r.json()

            if result.get("error"):
                err = result["error"]
                code = int(err.get("error_code") or 0)
                message = err.get("error_msg") or str(err)

                # Временные ошибки VK API.
                if code in (1, 6, 9, 10, 29):
                    raise RuntimeError(
                        f"VK API temporary error {code}: {message}"
                    )

                raise RuntimeError(
                    f"VK API error {code}: {message}"
                )

            print(
                f"VK CONFIRM OK attempt={attempt} "
                f"response={result.get('response')}",
                flush=True,
            )
            return result

        except Exception as exc:
            last_error = exc
            print(
                f"VK CONFIRM ERROR attempt={attempt}/{attempts}: {exc!r}",
                flush=True,
            )
            if attempt < attempts:
                time.sleep(1.2 * attempt)

    print(
        f"VK CONFIRM FAILED after {attempts} attempts: {last_error!r}",
        flush=True,
    )
    return None


def roll_artifact_direct(vk_id, name, plan, fact, report_id, started=None):
    """Бросок артефакта через нативное хранилище Google Sheets."""
    report_diag(report_id, "artifact_request_start", started, vk_id=str(vk_id))
    t0 = time.monotonic()
    result = digest_post({
        "action": "rollArtifact",
        "reportId": str(report_id),
        "vkId": str(vk_id),
        "name": str(name or ""),
        "plan": int(plan),
        "fact": int(fact),
        "forceArtifactDrop": str(vk_id) == "54341305",
    }, timeout=12)
    report_diag(
        report_id, "artifact_request_done", started,
        request_seconds=round(time.monotonic() - t0, 3),
        dropped=(result or {}).get("dropped"),
        chance=(result or {}).get("chance"),
    )
    return result


def send_report_confirmation_background(
    peer_id, vk_id, name, plan, fact, report_id, artifact_eligible=False
):
    """
    Формирует одно сообщение подтверждения.
    Отчёт к этому моменту уже сохранён. Если факт вырос, здесь в фоне
    выполняется бросок артефакта через нативный адаптер.
    """
    started = time.monotonic()
    report_diag(
        report_id, "confirmation_thread_start", started,
        artifact_eligible=artifact_eligible, vk_id=str(vk_id)
    )
    message = (
        f"🦉 Данные сохранены!\n{name}: "
        f"план {plan} 🐭, факт {fact} 🐭"
    )
    attachment = None

    if artifact_eligible and report_id:
        try:
            roll = roll_artifact_direct(vk_id, name, plan, fact, report_id, started)
            artifact = (roll or {}).get("artifact") or {}

            if (roll or {}).get("dropped") and artifact:
                intro = str(
                    artifact.get("intro") or "✨ Робосычик нашёл артефакт!"
                ).strip()
                emoji = str(artifact.get("emoji") or "").strip()
                label = str(artifact.get("label") or "Выпал артефакт").strip()
                artifact_name = str(
                    artifact.get("name") or "Неизвестный артефакт"
                ).strip()
                description = str(artifact.get("description") or "").strip()
                vk_photo = str(artifact.get("vkPhoto") or "").strip()

                artifact_lines = [
                    intro,
                    "",
                    f"{emoji} {label}".strip(),
                    artifact_name,
                ]
                if description:
                    artifact_lines += ["", f"«{description}»"]
                message += "\n\n" + "\n".join(artifact_lines)

                if re.fullmatch(
                    r"photo-?\d+_\d+", vk_photo, flags=re.IGNORECASE
                ):
                    attachment = vk_photo.lower()
        except Exception as exc:
            # Артефакт не должен задерживать или ломать подтверждение отчёта.
            print("ARTIFACT BACKGROUND ERROR:", repr(exc), flush=True)

    message = append_overplan_comment(message, plan, fact, report_id)
    report_diag(report_id, "vk_send_start", started, has_attachment=bool(attachment))
    send_t0 = time.monotonic()
    vk_send_reliable(peer_id, message, attachment=attachment, attempts=6)
    report_diag(
        report_id, "vk_send_done", started,
        send_seconds=round(time.monotonic() - send_t0, 3)
    )

def vk_get_chat_members(peer_id):
    """Получает актуальных пользователей беседы VK. Ничего не меняет."""
    r = requests.get(
        "https://api.vk.com/method/messages.getConversationMembers",
        params={
            "access_token": VK_TOKEN,
            "v": "5.199",
            "peer_id": peer_id,
        },
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(
            "VK getConversationMembers error: "
            + str(data["error"].get("error_msg", data["error"]))
        )

    response = data.get("response") or {}
    profiles = {
        str(p.get("id")): p
        for p in (response.get("profiles") or [])
        if p.get("id")
    }

    members = []
    for item in response.get("items") or []:
        member_id = item.get("member_id")
        if not member_id or int(member_id) <= 0:
            continue

        profile = profiles.get(str(member_id), {})
        name = " ".join(
            x for x in (
                str(profile.get("first_name") or "").strip(),
                str(profile.get("last_name") or "").strip(),
            )
            if x
        ).strip()

        members.append({
            "vkId": str(member_id),
            "name": name or str(member_id),
            "isAdmin": bool(item.get("is_admin")),
            "isOwner": bool(item.get("is_owner")),
        })

    return members


def sync_chat_participants(peer_id):
    """Синхронизирует Участники и текущую неделю с составом VK."""
    vk_members = vk_get_chat_members(peer_id)
    result = google_post({
        "action": "syncParticipants",
        "syncId": "chat-sync:" + uuid4().hex,
        "members": [{"vkId": p["vkId"], "name": p["name"]} for p in vk_members],
    })
    if not result.get("success"):
        raise RuntimeError(result.get("error", "Синхронизация не выполнена"))

    added = result.get("added") or []
    removed = result.get("removed") or []
    kept = result.get("keptReported") or []
    lines = [
        "🦉 Синхронизация состава завершена",
        "",
        f"Сейчас в VK: {result.get('vkCount', len(vk_members))}",
        f"В листе «Участники»: {result.get('participantsCount', '')}",
    ]
    if added:
        lines += ["", "➕ Добавлены новенькие:"]
        lines += [f"• {p.get('name')}" for p in added]
    if removed:
        lines += ["", "➖ Удалены из «Участников»:"]
        lines += [f"• {p.get('name')}" for p in removed]
    if kept:
        lines += ["", "📋 Оставлены в текущей неделе, потому что уже отчитались:"]
        lines += [f"• {p.get('name')}" for p in kept]
    if not added and not removed:
        lines += ["", "✅ Изменений нет, состав уже актуален."]
    return "\n".join(lines)


def vk_get_user(user_id):
    r = requests.get("https://api.vk.com/method/users.get", params={
        "access_token": VK_TOKEN, "v": "5.199", "user_ids": user_id
    }, timeout=10)
    r.raise_for_status()
    return (r.json().get("response") or [{}])[0]

def vk_get_name(user_id):
    try:
        p = vk_get_user(user_id)
        return " ".join(x for x in [p.get("first_name",""), p.get("last_name","")] if x).strip()
    except Exception as e:
        print("VK NAME ERROR:", repr(e))
        return str(user_id)

def text_after_bot_tag(text):
    gid = re.escape(VK_GROUP_ID)
    for p in [rf"^\s*\[club{gid}\|[^\]]+\]\s*", rf"^\s*@club{gid}\b[\s,:-]*"]:
        m = re.match(p, text, re.I)
        if m:
            return text[m.end():].strip()
    return None

def save_chat_message(msg, event_id):
    text = (msg.get("text") or "").strip()
    user_id = msg.get("from_id")
    if not text or not user_id or int(user_id) <= 0:
        return

    # Технический отчёт вида "Робосычик <план>,<факт>" не сохраняем
    # в буфер Сычевестника. Обсуждения и объяснения с таким примером
    # остаются, потому что фильтр требует полного совпадения сообщения.
    if PLAIN_REPORT_RE.fullmatch(text):
        print("DIGEST SKIP PLAIN REPORT:", text, flush=True)
        return

    digest_post({
        "action":"saveMessage",
        "timestamp":int(msg.get("date") or 0),
        "peerId":str(msg.get("peer_id")),
        "fromId":str(user_id),
        "name":vk_get_name(user_id),
        "text":text,
        "eventId":str(event_id or "")
    }, timeout=15)

def mark_event(event_id, user_id, peer_id):
    return google_post({
        "action":"markEventProcessed", "eventId":event_id,
        "vkId":str(user_id), "peerId":str(peer_id)
    })


def mark_event_background(event_id, user_id, peer_id, attempts=3):
    """
    Отмечает VK-событие обработанным, не блокируя callback.
    Сбой Google Apps Script не должен превращать ответ VK в HTTP 500.
    """
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            print(
                f"MARK EVENT ATTEMPT {attempt}/{attempts} "
                f"event_id={event_id}",
                flush=True,
            )
            result = mark_event(event_id, user_id, peer_id)
            print(
                f"MARK EVENT OK attempt={attempt} "
                f"event_id={event_id}",
                flush=True,
            )
            return result
        except Exception as exc:
            last_error = exc
            print(
                f"MARK EVENT ERROR attempt={attempt}/{attempts}: {exc!r}",
                flush=True,
            )
            if attempt < attempts:
                time.sleep(1.0 * attempt)

    print(
        f"MARK EVENT FAILED after {attempts} attempts: {last_error!r}",
        flush=True,
    )
    return None


def mark_event_async(event_id, user_id, peer_id):
    threading.Thread(
        target=mark_event_background,
        args=(event_id, user_id, peer_id),
        daemon=True,
    ).start()

def save_chat_message_background(msg, event_id):
    try:
        save_chat_message(msg, event_id)
    except Exception as e:
        print("BACKGROUND DIGEST SAVE ERROR:", repr(e))



def filter_digest_messages(messages):
    filtered = []
    skipped = 0
    for item in messages or []:
        msg_text = str(item.get("text") or "").strip()
        if PLAIN_REPORT_RE.fullmatch(msg_text):
            skipped += 1
            continue
        filtered.append(item)
    if skipped:
        print("SYCHEVESTNIK FILTERED REPORTS:", skipped, flush=True)
    return filtered

def build_scheduled_digest(messages, edition):
    digest = build_digest(messages)
    title = "🌅 Утренний #сычевестник" if edition == "morning" else "🌙 Вечерний #сычевестник"
    digest = re.sub(r"^📰\s*(?:\*\*)?Сычевестник(?:\*\*)?", title, digest.strip(), count=1, flags=re.IGNORECASE)
    if not digest.startswith(title):
        digest = title + "\n\n" + digest
    return digest


def generate_scheduled_digest(edition):
    peer_id = SYCHEVESTNIK_PEER_ID
    attachment = MORNING_PHOTO if edition == "morning" else EVENING_PHOTO

    print(
        "SYCHEVESTNIK START:",
        "edition=", edition,
        "peer_id=", peer_id,
        "attachment=", attachment,
        flush=True,
    )

    cutoff = datetime.now(ZoneInfo("Europe/Moscow")).replace(hour=18, minute=0, second=0, microsecond=0)
    if cutoff.timestamp() > time.time():
        cutoff -= timedelta(days=1)

    # Google Sheets API может временно отвечать медленно.
    # Не отменяем весь выпуск после первого ReadTimeout: пробуем до 3 раз.
    result = None
    last_fetch_error = None
    fetch_timeouts = (12, 20, 30)

    for attempt, fetch_timeout in enumerate(fetch_timeouts, start=1):
        try:
            print(
                f"SYCHEVESTNIK FETCH attempt {attempt}/{len(fetch_timeouts)} "
                f"timeout={fetch_timeout}s",
                flush=True,
            )
            result = digest_post(
                {
                    "action": "getEditionMessages",
                    "peerId": str(peer_id),
                    "until": cutoff.timestamp(),
                },
                timeout=fetch_timeout,
            )
            print(
                f"SYCHEVESTNIK FETCH OK attempt {attempt}/{len(fetch_timeouts)}",
                flush=True,
            )
            break
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_fetch_error = exc
            print(
                f"SYCHEVESTNIK FETCH TIMEOUT attempt {attempt}/{len(fetch_timeouts)}: {exc}",
                flush=True,
            )
            if attempt < len(fetch_timeouts):
                time.sleep(1.5)

    if result is None:
        raise last_fetch_error or RuntimeError(
            "Digest storage did not respond after 3 attempts"
        )
    if result.get("alreadySent"):
        return dict(success=True, skipped=True, reason="already_sent", edition=edition,
                    peer_id=peer_id, cutoff=cutoff.isoformat())
    messages = filter_digest_messages(result.get("messages") or [])
    print("SYCHEVESTNIK BUFFER:", len(messages), "messages", flush=True)

    digest = build_scheduled_digest(messages, edition)
    print("SYCHEVESTNIK DIGEST READY:", len(digest), "chars", flush=True)

    vk_result = vk_send(peer_id, digest, attachment=attachment)
    digest_post({"action": "completeEdition", "peerId": str(peer_id), "until": cutoff.timestamp()})
    print("SYCHEVESTNIK SENT:", vk_result, flush=True)

    return {
        "success": True,
        "edition": edition,
        "peer_id": peer_id,
        "messages": len(messages),
        "cutoff": cutoff.isoformat(),
        "attachment": attachment,
        "vk_response": vk_result.get("response"),
    }


def generate_digest_background(peer_id):
    try:
        result = digest_post({
            "action": "getMessages",
            "peerId": str(peer_id),
            "hours": 24,
        })
        messages = filter_digest_messages(result.get("messages") or [])
        digest = build_digest(messages)
        vk_send(peer_id, digest)
    except Exception as e:
        print("BACKGROUND DIGEST ERROR:", repr(e))
        try:
            vk_send(
                peer_id,
                "🦉 Не удалось собрать Сычевестник. "
                "Робосычик записал ошибку в журнал 🤖"
            )
        except Exception as send_error:
            print("BACKGROUND DIGEST SEND ERROR:", repr(send_error))


def generate_scheduled_stats():
    peer_id = STATS_PEER_ID
    print("WEEKLY STATS START:", "peer_id=", peer_id, flush=True)

    message = build_weekly_stats()
    vk_result = vk_send(peer_id, message)
    try:
        send_milestones(google_get, split_vk_message)
    except Exception as exc:
        print("QUEST MILESTONES ERROR:", repr(exc), flush=True)

    print(
        "WEEKLY STATS SENT:",
        "peer_id=", peer_id,
        "vk_response=", vk_result.get("response"),
        flush=True,
    )

    return {
        "success": True,
        "peer_id": peer_id,
        "vk_response": vk_result.get("response"),
    }


def generate_stats_background(peer_id):
    try:
        message = build_weekly_stats()
        vk_send(peer_id, message)
        try:
            send_milestones(google_get, split_vk_message)
        except Exception as exc:
            print("QUEST MILESTONES ERROR:", repr(exc), flush=True)
    except Exception as e:
        print("STATS ERROR:", repr(e), flush=True)
        try:
            vk_send(
                peer_id,
                "🦉 Не удалось получить статистику. "
                "Робосычик записал ошибку в журнал 🤖"
            )
        except Exception as send_error:
            print("STATS SEND ERROR:", repr(send_error), flush=True)


@app.get("/")
def health():
    from sheets_api import SHEET_ID
    return {"ok":True, "service":"sychnaya-ohota-native-sheets-v1", "schedulerVersion": 2, "questVotingVersion": 4, "reportRecoveryVersion": 3,
            "statsSpreadsheetMatchesExpected": SHEET_ID == "1S1tcMNZj84rLAQi_zPrPT-yxP0b6OYXHpo-JLbbbH60", "questLockRetryVersion": 1,
            "nativeEnabled":os.environ.get('NATIVE_STORAGE_ENABLED', '').lower() == 'true'}


@app.post('/native-schedule')
def native_schedule():
    supplied = request.headers.get('X-Schedule-Secret', '')
    if not SCHEDULE_SECRET or not hmac.compare_digest(supplied, SCHEDULE_SECRET):
        return jsonify(success=False, error='forbidden'), 403
    try:
        from native_jobs import due_jobs, run, configured_schedule
        data = request.get_json(silent=True) or {}
        requested = data.get('jobs')
        if requested is not None and (not isinstance(requested, list) or not requested
                or len(requested) > 3 or any(j not in ('evening', 'statistics', 'sync', 'quests') for j in requested)):
            return jsonify(success=False, error='Некорректный список заданий'), 400
        schedule = configured_schedule() if requested is None else []
        results = []
        for job in (list(dict.fromkeys(requested)) if requested is not None else due_jobs(schedule)):
            try:
                # У выпуска собственная отметка успешной отправки в _DigestState.
                # Явный вызов не зависит от старого расписания и окна 18:00–18:30.
                if job == 'evening' and requested is not None:
                    results.append(dict(job=job, **generate_scheduled_digest('evening')))
                elif job == 'quests' and not os.environ.get('QUEST_SPREADSHEET_ID', '').strip():
                    results.append(dict(success=True, job=job, skipped=True, reason='QUEST_SPREADSHEET_ID не задан'))
                else:
                    results.append(run(job))
            except Exception:
                app.logger.exception('Native scheduled job failed: %s', job)
                results.append(dict(success=False, job=job, error='Нужна проверка журнала _NativeJobs'))
        response = dict(success=all(r.get('success') for r in results), jobs=results)
        if requested is not None:
            response["schedulerVersion"] = 2
        return jsonify(response)
    except (ValueError, TypeError):
        return jsonify(success=False, error='Некорректная конфигурация расписания'), 503

@app.post("/sychevestnik")
def sychevestnik_schedule():
    supplied_secret = (
        request.headers.get("X-Schedule-Secret", "").strip()
        or request.args.get("secret", "").strip()
    )
    if not SCHEDULE_SECRET or supplied_secret != SCHEDULE_SECRET:
        return {
            "success": False,
            "stage": "auth",
            "error": "forbidden",
        }, 403

    data = request.get_json(silent=True) or {}
    edition = (
        data.get("edition")
        or request.args.get("edition")
        or ""
    ).strip().lower()

    if edition not in ("morning", "evening"):
        return {
            "success": False,
            "stage": "validation",
            "error": "edition must be morning or evening",
        }, 400

    try:
        result = generate_scheduled_digest(edition)
        return result, 200
    except requests.Timeout as e:
        print("SYCHEVESTNIK TIMEOUT:", repr(e), flush=True)
        return {
            "success": False,
            "stage": "request_timeout",
            "error": str(e),
        }, 504
    except Exception as e:
        print("SYCHEVESTNIK ERROR:", repr(e), flush=True)
        return {
            "success": False,
            "stage": "generation_or_send",
            "error": str(e),
        }, 500



# ================================================================
# НЕДЕЛЬНЫЕ БОНУСЫ: перечень получателей по факту из обычной
# статистики. Таблица с недельными результатами уже подключена через
# Google Sheets API; отдельное хранилище статистики бонусов не нужно.
# ================================================================

def _bonus_vk_id(value):
    """Не путаем ID ВКонтакте с номером строки таблицы."""
    value = str(value or "").strip()
    match = re.fullmatch(r"(?:https?://(?:www\.)?vk\.com/)?id(\d+)", value, re.I)
    if match:
        return match.group(1)
    if re.fullmatch(r"[1-9]\d*", value):
        return value
    return ""


def _bonus_field_id(item):
    """Только поля, которые явно означают VK ID (не generic id)."""
    for key in ("vkId", "vk_id", "vkID", "VK ID", "vkUserId", "vk_user_id"):
        value = _bonus_vk_id(item.get(key))
        if value:
            return value
    return ""


def _bonus_name(value):
    return " ".join(str(value or "").casefold().split())


def _bonus_fact(value):
    if isinstance(value, bool):
        return 0.0
    try:
        n = float(str(value or "0").replace("\xa0", "").replace(" ", "").replace(",", "."))
        return n if math.isfinite(n) else 0.0
    except (ValueError, TypeError):
        return 0.0


def build_weekly_bonus_candidates(threshold, week_selection="current"):
    """Определяет получателей, ничего не записывая и не отправляя в VK."""
    stats = google_get({"action": "bonusWeekStats", "week": week_selection})
    if not stats.get("success"):
        raise RuntimeError(stats.get("error") or "Не удалось получить недельную статистику")

    records = stats.get("currentRecords")
    if not isinstance(records, list):
        raise RuntimeError("Ответ статистики не содержит currentRecords — проверь основное Google API")

    week = stats.get("currentWeek") or {}
    week_key = str(week.get("start") or week.get("startDisplay") or "").strip()
    if not week_key:
        raise RuntimeError("Недельная статистика не вернула начало недели: защищаем от повторных наград")

    participants = stats.get("participants") or []
    if not participants:
        more = google_get({"action": "participants"})
        if not more.get("success"):
            raise RuntimeError(more.get("error") or "Не удалось получить список участников")
        participants = more.get("participants") or []

    by_name = {}
    for p in participants:
        if not isinstance(p, dict):
            continue
        vk_id = _bonus_field_id(p)
        name = _bonus_name(p.get("name"))
        if name:
            by_name.setdefault(name, set()).add(vk_id or "")

    eligible = {}
    unresolved = []
    for index, row in enumerate(records, start=1):
        if not isinstance(row, dict):
            continue
        fact = _bonus_fact(row.get("fact"))
        if fact <= threshold:  # Строго БОЛЬШЕ, не >=
            continue
        name = str(row.get("name") or "").strip()
        vk_id = _bonus_field_id(row)
        if not vk_id:
            match_ids = by_name.get(_bonus_name(name), set())
            if len(match_ids) == 1:
                vk_id = next(iter(match_ids))
        if not vk_id:
            unresolved.append({"name": name or f"Строка {index}", "fact": fact,
                               "reason": "Неоднозначное имя или отсутствует VK ID"})
            continue
        if vk_id in eligible:
            unresolved.append({"name": name or f"Строка {index}", "fact": fact,
                               "reason": "Повтор VK ID в недельных результатах: " + vk_id})
            continue
        eligible[vk_id] = {
            "vkId": vk_id,
            "name": name or vk_id,
            "fact": fact,
            "plan": _bonus_fact(row.get("plan")),
        }

    return {
        "success": True,
        "threshold": threshold,
        "selectedWeek": week_selection,
        "weekKey": week_key,
        "weekStart": str(week.get("startDisplay") or week.get("start") or ""),
        "weekEnd": str(week.get("endDisplay") or week.get("end") or ""),
        "recordCount": len(records),
        "eligible": list(eligible.values()),
        "unresolved": unresolved,
    }


@app.post("/weekly-artifact-candidates")
def weekly_artifact_candidates():
    supplied_secret = (request.headers.get("X-Schedule-Secret") or "").strip()
    if not SCHEDULE_SECRET or supplied_secret != SCHEDULE_SECRET:
        return jsonify({"success": False, "error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    try:
        threshold = float(data.get("threshold"))
        if not math.isfinite(threshold) or threshold < 0:
            raise ValueError("threshold должен быть неотрицательным числом")
    except (ValueError, TypeError) as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    week_selection = str(data.get("week") or "current").strip().lower()
    if week_selection not in ("current", "previous"):
        return jsonify({"success": False, "error": "week должен быть current или previous"}), 400
    try:
        result = build_weekly_bonus_candidates(threshold, week_selection)
        return jsonify(result), 200
    except Exception as exc:
        print("WEEKLY ARTIFACT CANDIDATES ERROR:", repr(exc), flush=True)
        return jsonify({"success": False, "error": str(exc)}), 500


@app.post("/statistics-schedule")
def statistics_schedule():
    supplied_secret = (
        request.headers.get("X-Schedule-Secret", "").strip()
        or request.args.get("secret", "").strip()
    )

    if not SCHEDULE_SECRET or supplied_secret != SCHEDULE_SECRET:
        return {
            "success": False,
            "stage": "auth",
            "error": "forbidden",
        }, 403

    try:
        return generate_scheduled_stats(), 200
    except requests.Timeout as e:
        print("WEEKLY STATS TIMEOUT:", repr(e), flush=True)
        return {
            "success": False,
            "stage": "request_timeout",
            "error": str(e),
        }, 504
    except Exception as e:
        print("WEEKLY STATS ERROR:", repr(e), flush=True)
        return {
            "success": False,
            "stage": "generation_or_send",
            "error": str(e),
        }, 500


def vote_feedback(user_id, peer_id, event_id, text):
    """Уведомление участнику; подтверждения и ошибки не публикуются в беседе."""
    try:
        vk_send(int(user_id), text, random_id=f'vote-dm:{user_id}:{peer_id}:{event_id}' if event_id else None)
    except Exception:
        app.logger.warning('VK не подтвердил личное уведомление о голосе')
    if event_id:
        try:
            response = requests.post('https://api.vk.com/method/messages.sendMessageEventAnswer',
                data={'access_token': VK_TOKEN, 'v': '5.199', 'event_id': event_id,
                      'user_id': int(user_id), 'peer_id': int(peer_id),
                      'event_data': json.dumps({'type': 'show_snackbar', 'text': text[:90]}, ensure_ascii=False)},
                timeout=10)
            response.raise_for_status()
            if response.json().get('error'):
                app.logger.warning('VK не подтвердил уведомление возле кнопки')
        except Exception:
            app.logger.warning('Не удалось подтвердить уведомление возле кнопки')


def process_quest_vote_callback(event):
    from quest_votes import parse_payload
    from quest_engine import quest_api
    try:
        user_id, peer_id = int(event.get('user_id') or 0), int(event.get('peer_id') or 0)
        event_id = event.get('event_id')
        selection = parse_payload(event.get('payload'))
        if (not selection or user_id <= 0 or peer_id < 2000000000
                or not isinstance(event_id, str) or not 1 <= len(event_id) <= 256):
            return
    except (ValueError, TypeError):
        return
    try:
        result = quest_api('vote', **selection, peerId=str(peer_id), vkId=str(user_id),
                           source='callback', eventId=event_id)
        text = ('🗳 Голос ' + ('изменён' if result.get('changed') else 'принят') +
                f": вариант {result['choice']}.")
        if result.get('duplicate'):
            text = f"🗳 Твой текущий голос: вариант {result['choice']}."
    except ValueError as exc:
        text = '🗳 ' + str(exc)
    except Exception:
        app.logger.exception('Не удалось сохранить голос с callback-кнопки')
        text = '🗳 Голос не сохранён. Нажми кнопку ещё раз чуть позже.'
    vote_feedback(user_id, peer_id, event_id, text)


def process_vk_event(data):
    """Обработка события вне HTTP-запроса VK."""
    try:
        if data.get("type") == "confirmation":
            return Response(VK_CONFIRMATION_CODE, mimetype="text/plain")
        if data.get("type") not in ("message_new", "message_event"):
            return Response("ok")
        if str(data.get("group_id","")) != VK_GROUP_ID:
            return Response("ok")
        if data.get("type") == "message_event":
            process_quest_vote_callback(data.get("object") or {})
            return Response("ok")

        msg = ((data.get("object") or {}).get("message") or {})
        user_id = msg.get("from_id")
        peer_id = msg.get("peer_id")
        text = (msg.get("text") or "").strip()
        event_id = data.get("event_id")

        if not user_id or not peer_id or int(peer_id) < 2000000000:
            return Response("ok")

        # Inline text-кнопки доставляются обычным message_new с JSON payload.
        # Пользователь, чат и порядок берутся только из события VK.
        from quest_votes import parse_payload
        try:
            selection = parse_payload(msg.get('payload'))
        except ValueError:
            return Response('ok')
        if selection is not None:
            if int(user_id) <= 0:
                return Response("ok")
            from quest_engine import quest_api
            try:
                result = quest_api('vote', **selection, peerId=str(peer_id), vkId=str(user_id),
                    messageId=int(msg.get('conversation_message_id') or msg.get('id') or 0))
            except ValueError as exc:
                vote_feedback(user_id, peer_id, None, '🗳 ' + str(exc))
                return Response("ok")
            except Exception:
                app.logger.exception('Не удалось сохранить голос')
                vote_feedback(user_id, peer_id, None, '🗳 Не удалось сохранить голос. Нажми кнопку ещё раз чуть позже.')
                return Response("ok")
            if not result.get('duplicate'):
                vote_feedback(user_id, peer_id, None, "🗳 Голос " +
                    ('изменён' if result.get('changed') else 'принят') + f": вариант {result['choice']}.")
            return Response("ok")

        if SAVE_CHAT_MESSAGES:
            threading.Thread(
                target=save_chat_message_background,
                args=(dict(msg), event_id),
                daemon=True,
            ).start()

        # Отчёт без VK-тега:
        # "Робосычик 10,6", регистр и количество пробелов не важны.
        # Первое число = план, второе = факт.
        plain_report = PLAIN_REPORT_RE.match(text)
        if plain_report:
            if not command_event_once(event_id):
                return Response("ok")

            plan, fact = map(int, plain_report.groups())
            report_id = f"vk-{event_id}"
            result = google_save_report(
                user_id, plan, fact, report_id, attempts=3
            )

            if result.get("unknownVkUser"):
                profile = vk_get_user(user_id)
                bind = google_post({
                    "action": "vkAutoBind",
                    "vkId": str(user_id),
                    "firstName": profile.get("first_name", ""),
                    "lastName": profile.get("last_name", ""),
                })
                if bind.get("success"):
                    result = google_save_report(
                        user_id, plan, fact, report_id, attempts=3
                    )

            if result.get("success"):
                mark_event_async(event_id, user_id, peer_id)
                print(
                    f"REPORT SAVED: {result.get('name','')} "
                    f"plan={plan} fact={fact}",
                    flush=True,
                )
                threading.Thread(
                    target=send_report_confirmation_background,
                    args=(
                        peer_id,
                        user_id,
                        result.get("name", ""),
                        plan,
                        fact,
                        report_id,
                        bool(result.get("artifactEligible")),
                    ),
                    daemon=True,
                ).start()
            else:
                vk_send(peer_id, "🦉 Не удалось подтвердить сохранение отчёта. Повтори его чуть позже.")

            return Response("ok")

        payload = text_after_bot_tag(text)
        if payload is None:
            return Response("ok")

        if payload.lower() == "дайджест":
            threading.Thread(
                target=generate_digest_background,
                args=(peer_id,),
                daemon=True,
            ).start()

            return Response("ok")

        if payload.lower() in ("синхронизация", "проверка участников", "участники проверить"):
            if not command_event_once(event_id):
                return Response("ok")

            def _participants_preview_job():
                try:
                    preview = sync_chat_participants(peer_id)
                    vk_send(peer_id, preview)
                except Exception as exc:
                    print("PARTICIPANTS SYNC ERROR:", repr(exc), flush=True)
                    try:
                        vk_send(
                            peer_id,
                            "🦉 Не удалось синхронизировать состав участников. "
                            "Ошибка записана в журнал Робосычика 🤖"
                        )
                    except Exception as send_exc:
                        print("PARTICIPANTS SYNC SEND ERROR:", repr(send_exc), flush=True)

            threading.Thread(
                target=_participants_preview_job,
                daemon=True,
            ).start()
            return Response("ok")

        if payload.lower() == "статистика":
            if not command_event_once(event_id):
                return Response("ok")

            threading.Thread(
                target=generate_stats_background,
                args=(peer_id,),
                daemon=True,
            ).start()
            return Response("ok")

        if payload.lower() == "тест буфера":
            try:
                result = digest_post({"action":"getMessages","peerId":str(peer_id),"hours":24})
                vk_send(peer_id,
                    "🦉 Буфер Робосычика работает!\n\n"
                    f"В этом чате сохранено сообщений за последние 24 часа: {int(result.get('count',0))}\n"
                    "Чаты друг с другом не смешиваются 🤖")
            except Exception as e:
                print("BUFFER TEST ERROR:", repr(e))
                try:
                    vk_send(peer_id, f"🦉 Тест буфера не пройден.\n\nОшибка: {e}")
                except Exception:
                    pass
            return Response("ok")

        check = google_post({
            "action":"checkEvent","eventId":event_id,
            "vkId":str(user_id),"peerId":str(peer_id)
        })
        if check.get("exists"):
            return Response("ok")

        m = PAIR_RE.match(payload)
        if not m:
            vk_send(peer_id, "🦉 Формат: план, факт\nНапример: 30, 17")
            return Response("ok")

        plan, fact = map(int, m.groups())
        report_id = f"vk-{event_id}"
        result = google_save_report(
            user_id, plan, fact, report_id, attempts=3
        )

        if result.get("unknownVkUser"):
            profile = vk_get_user(user_id)
            bind = google_post({
                "action":"vkAutoBind","vkId":str(user_id),
                "firstName":profile.get("first_name",""),
                "lastName":profile.get("last_name","")
            })
            if bind.get("success"):
                result = google_save_report(
                    user_id, plan, fact, report_id, attempts=3
                )

        if result.get("success"):
            mark_event_async(event_id, user_id, peer_id)
            print(
                f"REPORT SAVED: {result.get('name','')} "
                f"plan={plan} fact={fact}",
                flush=True,
            )
            threading.Thread(
                    target=send_report_confirmation_background,
                    args=(
                        peer_id,
                        user_id,
                        result.get("name", ""),
                        plan,
                        fact,
                        report_id,
                        bool(result.get("artifactEligible")),
                    ),
                    daemon=True,
                ).start()
        else:
            vk_send(peer_id, "🦉 Не удалось подтвердить сохранение отчёта. Повтори его чуть позже.")

        return Response("ok")
    except Exception as e:
        print("VK EVENT PROCESSING FAILED:", repr(e), "event_id=", data.get("event_id"), flush=True)
        return Response("ok")


@app.post("/vk")
def vk_callback():
    """Немедленно подтверждает Callback API, не ожидая Google Apps Script."""
    data = request.get_json(silent=True) or {}
    if data.get("type") == "confirmation":
        return Response(VK_CONFIRMATION_CODE, mimetype="text/plain")
    if data.get("type") not in ("message_new", "message_event"):
        return Response("ok", mimetype="text/plain")
    if str(data.get("group_id", "")) != VK_GROUP_ID:
        return Response("ok", mimetype="text/plain")

    msg = (data.get("object") or {}).get("message") or {}
    print(
        "VK RECEIVED",
        "event_id=", data.get("event_id"),
        "message_id=", msg.get("id"),
        "peer_id=", msg.get("peer_id"),
        "text=", repr((msg.get("text") or "")[:160]),
        flush=True,
    )
    threading.Thread(
        target=process_vk_event,
        args=(data,),
        daemon=True,
        name="vk-event-worker",
    ).start()
    return Response("ok", mimetype="text/plain")


@app.route("/send-owlet-message", methods=["POST"])
def send_owlet_message_route():
    try:
        data = request.get_json(silent=True) or {}

        if str(data.get("secret") or "") != str(SCHEDULE_SECRET or ""):
            return jsonify({"success": False, "error": "unauthorized"}), 403

        peer_id = int(data.get("peer_id") or SYCHEVESTNIK_PEER_ID)
        message = str(data.get("text") or "").strip()
        attachment = str(data.get("attachment") or "").strip()

        if not message and not attachment:
            return jsonify({
                "success": False,
                "error": "Нужен текст или attachment"
            }), 400

        print(
            "MANUAL OWLET POST:",
            "peer_id=", peer_id,
            "text_len=", len(message),
            "attachment=", attachment,
            flush=True,
        )

        vk_send(peer_id, message, attachment=attachment or None)

        return jsonify({
            "success": True,
            "peer_id": peer_id,
            "has_text": bool(message),
            "attachment": attachment,
        }), 200

    except Exception as exc:
        print("MANUAL OWLET POST ERROR:", repr(exc), flush=True)
        return jsonify({"success": False, "error": str(exc)}), 500




def google_save_report(vk_id, plan, fact, report_id, attempts=3):
    from website_store import Busy
    payload = {"action": "vkSave", "vkId": str(vk_id), "plan": int(plan),
               "fact": int(fact), "reportId": str(report_id)}
    for attempt in range(max(1, int(attempts))):
        try:
            return google_post(payload)
        except Busy:
            if attempt + 1 < max(1, int(attempts)):
                time.sleep(.5 * (attempt + 1))
                continue
            app.logger.exception('REPORT SAVE BUSY report_id=%s', report_id)
        except Exception:
            # Не повторяем неопределённую запись. Отчёт должен получить ответ
            # в беседе, даже если Google не подтвердил сохранение.
            app.logger.exception('REPORT SAVE FAILED report_id=%s', report_id)
        return {'success': False, 'error': 'Сохранение отчёта не подтверждено'}

def google_sync_participants(members, sync_id):
    return google_post({"action": "syncParticipants", "members": members, "syncId": sync_id})
    

@app.route("/sync-participants-silent", methods=["POST"])
def sync_participants_silent_route():
    try:
        data = request.get_json(silent=True) or {}
        if str(data.get("secret") or "") != str(SCHEDULE_SECRET or ""):
            return jsonify({"success": False, "error": "unauthorized"}), 403

        peer_id = int(data.get("peer_id") or SYCHEVESTNIK_PEER_ID)
        print("PARTICIPANTS SILENT SYNC START:", peer_id, flush=True)

        # Та же рабочая синхронизация, что и у команды в VK.
        # Возвращаем результат только вызывающему Apps Script, в VK ничего не отправляем.
        vk_members = vk_get_chat_members(peer_id)
        sync_id = str(data.get("sync_id") or "").strip() or (
            f"sync-{peer_id}-{int(time.time())}"
        )
        result = google_sync_participants(
            [
                {"vkId": p["vkId"], "name": p["name"]}
                for p in vk_members
            ],
            sync_id,
        )

        if not result.get("success"):
            raise RuntimeError(result.get("error", "Синхронизация не выполнена"))

        print(
            "PARTICIPANTS SILENT SYNC OK:",
            "vk=", result.get("vkCount"),
            "participants=", result.get("participantsCount"),
            "added=", len(result.get("added") or []),
            "removed=", len(result.get("removed") or []),
            "keptReported=", len(result.get("keptReported") or []),
            flush=True,
        )
        return jsonify(result), 200

    except Exception as exc:
        print("PARTICIPANTS SILENT SYNC ERROR:", repr(exc), flush=True)
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 500


@app.post("/quest-tick")
def quest_tick_route():
    supplied = request.headers.get("X-Schedule-Secret", "").strip()
    if not SCHEDULE_SECRET or supplied != SCHEDULE_SECRET:
        return jsonify({"success": False, "error": "forbidden"}), 403
    try:
        data = request.get_json(silent=True) or {}
        result = quest_tick(google_get, vk_send, STATS_PEER_ID,
                            dry_run=data.get("dry_run") is True)
        return jsonify(result), 200
    except Exception as exc:
        app.logger.exception("QUEST TICK ERROR")
        return jsonify({"success": False, "error": str(exc)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

