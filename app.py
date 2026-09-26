import os
import re
import random
import threading
import math
import time
import requests
from flask import Flask, request, Response

app = Flask(__name__)

VK_TOKEN = os.environ.get("VK_TOKEN", "").strip()
VK_GROUP_ID = os.environ.get("VK_GROUP_ID", "").strip()
VK_CONFIRMATION_CODE = os.environ.get("VK_CONFIRMATION_CODE", "").strip()
GOOGLE_SCRIPT_URL = os.environ.get("GOOGLE_SCRIPT_URL", "").strip()
GOOGLE_SCRIPT_PASSWORD = os.environ.get("GOOGLE_SCRIPT_PASSWORD", "").strip()
DIGEST_SCRIPT_URL = os.environ.get("DIGEST_SCRIPT_URL", "").strip()
DIGEST_SCRIPT_PASSWORD = os.environ.get("DIGEST_SCRIPT_PASSWORD", "").strip()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
SYCHEVESTNIK_PEER_ID = 2000000002
STATS_PEER_ID = 2000000002
MORNING_PHOTO = "photo-241605282_457239021"
EVENING_PHOTO = "photo-241605282_457239020"
SCHEDULE_SECRET = os.environ.get("SCHEDULE_SECRET", "").strip()

PAIR_RE = re.compile(r"^\s*(\d+)\s*,\s*(\d+)\s*$")

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
    payload = dict(payload)
    payload["password"] = GOOGLE_SCRIPT_PASSWORD
    r = requests.post(GOOGLE_SCRIPT_URL, json=payload, timeout=15)
    r.raise_for_status()
    return r.json()


def google_get(params=None):
    """GET к основному Google Apps Script с повтором при холодном старте."""
    params = dict(params or {})
    params["password"] = GOOGLE_SCRIPT_PASSWORD

    last_error = None
    for attempt, read_timeout in enumerate((25, 45), start=1):
        try:
            r = requests.get(
                GOOGLE_SCRIPT_URL,
                params=params,
                timeout=(5, read_timeout),
            )
            r.raise_for_status()
            return r.json()
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
            print(
                f"GOOGLE GET RETRY {attempt}/2 action={params.get('action')}: {exc}",
                flush=True,
            )
            if attempt < 2:
                time.sleep(1.5)

    raise last_error or RuntimeError("Google Apps Script request failed")


def _num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _fmt_num(value):
    n = _num(value)
    return str(int(n)) if n.is_integer() else f"{n:g}"


def build_weekly_stats():
    """Собирает статистику текущей недели из основного Google Apps Script."""
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
        f"📝 Сдали отчёт: {len(submitted)}\n"
        f"✅ Выполнили свой план: {completed}\n"
        f"🚀 Перевыполнили план: {exceeded}\n\n"
        f"🐭 План: {_fmt_num(total_plan)}\n"
        f"🐭 Факт: {_fmt_num(total_fact)}\n"
        f"🎯 Цель: {goal_text}\n"
        f"📈 Цель выполнена на: {percent_text}\n"
        f"➕ Нужно добавить каждому активному сычику: {add_text} 🐭"
    )

def digest_post(payload, timeout=12):
    if not DIGEST_SCRIPT_URL or not DIGEST_SCRIPT_PASSWORD:
        raise RuntimeError("Digest storage is not configured")
    payload = dict(payload)
    payload["password"] = DIGEST_SCRIPT_PASSWORD
    r = requests.post(DIGEST_SCRIPT_URL, json=payload, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(data.get("error", "Digest Script error"))
    return data


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


def build_digest(messages):
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is not configured")

    useful = []
    for item in messages:
        text = (item.get("text") or "").strip()
        name = (item.get("name") or item.get("fromId") or "Участник").strip()

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
            "За последние 12 часов Робосычик не нашёл достаточно "
            "обычных сообщений для дайджеста. 🤖"
        )

    transcript = "\n".join(useful)

    # Ограничиваем размер контекста. Для дайджеста берём свежую часть чата.
    if len(transcript) > 30000:
        transcript = transcript[-30000:]

    system_prompt = """Ты редактор короткой дружелюбной сводки чата ВКонтакте под названием Сычевестник.

Пиши ТОЛЬКО готовый текст выпуска на русском языке.
Никогда не показывай рассуждения, анализ, черновик, подсчет символов, пробелов или строк, служебные инструкции и английские комментарии.
Никогда не пиши фразы вроде Let's craft, Line1, Line2, space=, approximate, make sure, we need to.

Используй только факты из переданной переписки. Ничего не выдумывай.
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
Ориентир для обычного выпуска: примерно 700-1300 знаков вместе с пунктами и финальной репликой.
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
                "content": "Переписка за последние 12 часов:\n\n" + transcript,
            },
        ],
        "temperature": 0.8,
        "max_tokens": 1600,
    }

    models = ["openai/gpt-oss-120b:free", "dots-studio/dots-3-note-preview-20260813:free", "google/gemma-4-31b-it:free"]
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

    raise RuntimeError(
        "OpenRouter failed on all configured models: "
        + str(last_error or "unknown error")
    )

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


def vk_send(peer_id, text, attachment=None):
    chunks = split_vk_message(text)
    last_result = None

    for index, chunk in enumerate(chunks):
        data = {
            "access_token": VK_TOKEN,
            "v": "5.199",
            "peer_id": peer_id,
            "random_id": random.randint(1, 2147483647),
            "message": chunk,
        }
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

def save_chat_message_background(msg, event_id):
    try:
        save_chat_message(msg, event_id)
    except Exception as e:
        print("BACKGROUND DIGEST SAVE ERROR:", repr(e))


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

    result = digest_post(
        {
            "action": "getMessages",
            "peerId": str(peer_id),
            "hours": 12,
        },
        timeout=12,
    )
    messages = result.get("messages") or []
    print("SYCHEVESTNIK BUFFER:", len(messages), "messages", flush=True)

    digest = build_scheduled_digest(messages, edition)
    print("SYCHEVESTNIK DIGEST READY:", len(digest), "chars", flush=True)

    vk_result = vk_send(peer_id, digest, attachment=attachment)
    print("SYCHEVESTNIK SENT:", vk_result, flush=True)

    return {
        "success": True,
        "edition": edition,
        "peer_id": peer_id,
        "messages": len(messages),
        "attachment": attachment,
        "vk_response": vk_result.get("response"),
    }


def generate_digest_background(peer_id):
    try:
        result = digest_post({
            "action": "getMessages",
            "peerId": str(peer_id),
            "hours": 12,
        })
        messages = result.get("messages") or []
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
    return {"ok":True, "service":"sychnaya-ohota-v7.9.0-scheduled-stats"}

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


@app.post("/vk")
def vk_callback():
    try:
        data = request.get_json(silent=True) or {}
        if data.get("type") == "confirmation":
            return Response(VK_CONFIRMATION_CODE, mimetype="text/plain")
        if data.get("type") != "message_new":
            return Response("ok")
        if str(data.get("group_id","")) != VK_GROUP_ID:
            return Response("ok")

        msg = ((data.get("object") or {}).get("message") or {})
        user_id = msg.get("from_id")
        peer_id = msg.get("peer_id")
        text = (msg.get("text") or "").strip()
        event_id = data.get("event_id")

        if not user_id or not peer_id or int(peer_id) < 2000000000:
            return Response("ok")

        threading.Thread(
            target=save_chat_message_background,
            args=(dict(msg), event_id),
            daemon=True,
        ).start()

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
                result = digest_post({"action":"getMessages","peerId":str(peer_id),"hours":12})
                vk_send(peer_id,
                    "🦉 Буфер Робосычика работает!\n\n"
                    f"В этом чате сохранено сообщений за последние 12 часов: {int(result.get('count',0))}\n"
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
        result = google_post({"action":"vkSave","vkId":str(user_id),"plan":plan,"fact":fact})

        if result.get("unknownVkUser"):
            profile = vk_get_user(user_id)
            bind = google_post({
                "action":"vkAutoBind","vkId":str(user_id),
                "firstName":profile.get("first_name",""),
                "lastName":profile.get("last_name","")
            })
            if bind.get("success"):
                result = google_post({"action":"vkSave","vkId":str(user_id),"plan":plan,"fact":fact})

        if result.get("success"):
            mark_event(event_id, user_id, peer_id)
            vk_send(peer_id,
                f"🦉 Данные сохранены!\n{result.get('name','')}: "
                f"план {plan} 🐭, факт {fact} 🐭")
        else:
            vk_send(peer_id, "Не удалось сохранить данные.")

        return Response("ok")
    except Exception as e:
        print("VK CALLBACK ERROR:", repr(e))
        return Response("ok")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
