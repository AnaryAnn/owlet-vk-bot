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


def vk_send(user_id, text):
    if not VK_TOKEN:
        raise RuntimeError("VK_TOKEN is not set")

    r = requests.post(
        "https://api.vk.com/method/messages.send",
        data={
            "access_token": VK_TOKEN,
            "v": "5.199",
            "user_id": user_id,
            "random_id": random.randint(1, 2_147_483_647),
            "message": text,
        },
        timeout=10,
    )
    r.raise_for_status()
    payload = r.json()

    if "error" in payload:
        raise RuntimeError(str(payload["error"]))


def google_post(payload):
    if not GOOGLE_SCRIPT_URL or not GOOGLE_SCRIPT_PASSWORD:
        raise RuntimeError("Google Script settings are not set")

    payload = dict(payload)
    payload["password"] = GOOGLE_SCRIPT_PASSWORD

    r = requests.post(
        GOOGLE_SCRIPT_URL,
        json=payload,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


@app.get("/")
def health():
    return {"ok": True, "service": "sychnaya-ohota-vk-bot"}


@app.post("/vk")
def vk_callback():
    data = request.get_json(silent=True) or {}

    # VK asks for this when confirming Callback API server.
    if data.get("type") == "confirmation":
        return Response(VK_CONFIRMATION_CODE, mimetype="text/plain")

    # Ignore events from another community if group_id is configured.
    if VK_GROUP_ID and str(data.get("group_id", "")) != VK_GROUP_ID:
        return Response("ok", mimetype="text/plain")

    if data.get("type") != "message_new":
        return Response("ok", mimetype="text/plain")

    obj = data.get("object") or {}
    message = obj.get("message") or {}
    user_id = message.get("from_id")
    text = (message.get("text") or "").strip()

    if not user_id:
        return Response("ok", mimetype="text/plain")

    match = PAIR_RE.match(text)

    if not match:
        try:
            vk_send(
                user_id,
                "🦉 Отправь план и факт через запятую.\n"
                "Например: 30, 17"
            )
        except Exception as exc:
            print("VK send error:", exc)
        return Response("ok", mimetype="text/plain")

    plan = int(match.group(1))
    fact = int(match.group(2))

    try:
        result = google_post({
            "action": "vkSave",
            "vkId": str(user_id),
            "plan": plan,
            "fact": fact,
        })

        if result.get("unknownVkUser"):
            vk_send(
                user_id,
                "🦉 Я пока не знаю, кто ты.\n"
                f"Твой VK ID: {user_id}\n"
                "Передай этот номер организатору."
            )
        elif result.get("success"):
            name = result.get("name", "")
            vk_send(
                user_id,
                "🦉 Данные сохранены!\n\n"
                f"{name}\n"
                f"План: {plan} 🐭\n"
                f"Факт: {fact} 🐭"
            )
        else:
            vk_send(
                user_id,
                "Не удалось сохранить данные. Попробуй немного позже."
            )

    except Exception as exc:
        print("Processing error:", exc)
        try:
            vk_send(
                user_id,
                "Не удалось сохранить данные. Попробуй немного позже."
            )
        except Exception:
            pass

    # VK expects a fast plain-text "ok".
    return Response("ok", mimetype="text/plain")
