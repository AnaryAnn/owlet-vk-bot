import os
import re
import random
import requests
from flask import Flask, request, Response

app = Flask(__name__)

VK_TOKEN = os.environ.get("VK_TOKEN", "").strip()
VK_GROUP_ID = os.environ.get("VK_GROUP_ID", "").strip()
VK_CONFIRMATION_CODE = os.environ.get("VK_CONFIRMATION_CODE", "").strip()

GOOGLE_SCRIPT_URL = os.environ.get("GOOGLE_SCRIPT_URL", "").strip()
GOOGLE_SCRIPT_PASSWORD = os.environ.get("GOOGLE_SCRIPT_PASSWORD", "").strip()

PAIR_RE = re.compile(r"^\s*(\d+)\s*,\s*(\d+)\s*$")


def vk_send(peer_id, text):
    r = requests.post(
        "https://api.vk.com/method/messages.send",
        data={
            "access_token": VK_TOKEN,
            "v": "5.199",
            "peer_id": peer_id,
            "random_id": random.randint(1, 2147483647),
            "message": text,
        },
        timeout=10,
    )

    r.raise_for_status()

    result = r.json()

    if "error" in result:
        raise RuntimeError(str(result["error"]))


def vk_get_user(user_id):
    r = requests.get(
        "https://api.vk.com/method/users.get",
        params={
            "access_token": VK_TOKEN,
            "v": "5.199",
            "user_ids": user_id,
        },
        timeout=10,
    )

    r.raise_for_status()

    payload = r.json()

    if "error" in payload:
        raise RuntimeError(str(payload["error"]))

    users = payload.get("response") or []

    return users[0] if users else {}


def google_post(payload):
    payload = dict(payload)
    payload["password"] = GOOGLE_SCRIPT_PASSWORD

    r = requests.post(
        GOOGLE_SCRIPT_URL,
        json=payload,
        timeout=15,
    )

    r.raise_for_status()

    return r.json()


def check_event(event_id, user_id, peer_id):
    if not event_id:
        return False

    result = google_post(
        {
            "action": "checkEvent",
            "eventId": event_id,
            "vkId": str(user_id),
            "peerId": str(peer_id),
        }
    )

    return result.get("exists", False)


def mark_event(event_id, user_id, peer_id):
    if not event_id:
        return

    google_post(
        {
            "action": "markEventProcessed",
            "eventId": event_id,
            "vkId": str(user_id),
            "peerId": str(peer_id),
        }
    )


def text_after_bot_tag(text):
    if not VK_GROUP_ID:
        return None

    gid = re.escape(VK_GROUP_ID)

    patterns = [
        rf"^\s*\[club{gid}\|[^\]]+\]\s*",
        rf"^\s*@club{gid}\b[\s,:-]*",
    ]

    for pattern in patterns:

        match = re.match(
            pattern,
            text,
            flags=re.IGNORECASE
        )

        if match:
            return text[match.end():].strip()

    return None


@app.get("/")
def health():
    return {
        "ok": True,
        "service": "sychnaya-ohota-v6.2"
    }


@app.post("/vk")
def vk_callback():

    data = request.get_json(silent=True) or {}


    # подтверждение VK
    if data.get("type") == "confirmation":

        return Response(
            VK_CONFIRMATION_CODE,
            mimetype="text/plain"
        )


    # проверка группы

    if VK_GROUP_ID and str(data.get("group_id", "")) != VK_GROUP_ID:
        return Response("ok", mimetype="text/plain")


    # только сообщения

    if data.get("type") != "message_new":
        return Response("ok", mimetype="text/plain")


    message = (
        (data.get("object") or {})
        .get("message") or {}
    )


    user_id = message.get("from_id")
    peer_id = message.get("peer_id")
    text = (message.get("text") or "").strip()


    if not user_id or not peer_id:
        return Response("ok", mimetype="text/plain")


    # только беседы

    if int(peer_id) < 2000000000:
        return Response("ok", mimetype="text/plain")


    # защита от дублей

    event_id = data.get("event_id")

    try:

        if check_event(event_id, user_id, peer_id):

            print(
                "Duplicate event ignored:",
                event_id
            )

            return Response(
                "ok",
                mimetype="text/plain"
            )


        # проверяем тег

        payload_text = text_after_bot_tag(text)


        if payload_text is None:
            return Response(
                "ok",
                mimetype="text/plain"
            )


        match = PAIR_RE.match(payload_text)


        if not match:

            vk_send(
                peer_id,
                "🦉 После упоминания бота отправь план и факт через запятую.\n"
                "Например: @Робосычик 30, 17"
            )

            return Response(
                "ok",
                mimetype="text/plain"
            )


        plan, fact = map(
            int,
            match.groups()
        )


        result = google_post(
            {
                "action": "vkSave",
                "vkId": str(user_id),
                "plan": plan,
                "fact": fact,
            }
        )


        # новый пользователь

        if result.get("unknownVkUser"):

            profile = vk_get_user(user_id)


            bind_result = google_post(
                {
                    "action": "vkAutoBind",
                    "vkId": str(user_id),
                    "firstName": profile.get("first_name", ""),
                    "lastName": profile.get("last_name", ""),
                }
            )


            if bind_result.get("success"):

                result = google_post(
                    {
                        "action": "vkSave",
                        "vkId": str(user_id),
                        "plan": plan,
                        "fact": fact,
                    }
                )


                vk_send(
                    peer_id,
                    f"🦉 Нашла тебя: {bind_result.get('name')}\n"
                    f"Данные сохранены: план {plan} 🐭, факт {fact} 🐭"
                )


            elif bind_result.get("ambiguous"):

                vk_send(
                    peer_id,
                    "🦉 Не смогла однозначно определить участника."
                )

                return Response(
                    "ok",
                    mimetype="text/plain"
                )


            else:

                vk_send(
                    peer_id,
                    f"🦉 Не нашла тебя в списке участников.\n"
                    f"VK ID: {user_id}"
                )

                return Response(
                    "ok",
                    mimetype="text/plain"
                )


        elif result.get("success"):

            vk_send(
                peer_id,
                f"🦉 Данные сохранены!\n"
                f"{result.get('name','')}: "
                f"план {plan} 🐭, факт {fact} 🐭"
            )


        else:

            vk_send(
                peer_id,
                "Не удалось сохранить данные."
            )

            return Response(
                "ok",
                mimetype="text/plain"
            )


        # только после успешного сохранения
        mark_event(
            event_id,
            user_id,
            peer_id
        )


    except Exception as exc:

        print(
            "PROCESSING ERROR:",
            repr(exc)
        )

        try:
            vk_send(
                peer_id,
                "Не удалось сохранить данные. Попробуй позже."
            )

        except Exception:
            pass


    return Response(
        "ok",
        mimetype="text/plain"
    )
