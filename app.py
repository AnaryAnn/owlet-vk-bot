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


def google_post(payload):
    payload = dict(payload)
    payload["password"] = GOOGLE_SCRIPT_PASSWORD
    r = requests.post(GOOGLE_SCRIPT_URL, json=payload, timeout=15)
    r.raise_for_status()
    return r.json()


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


def vk_get_user(user_id):
    r = requests.get(
        "https://api.vk.com/method/users.get",
        params={"access_token": VK_TOKEN, "v": "5.199", "user_ids": user_id},
        timeout=10,
    )
    r.raise_for_status()
    return (r.json().get("response") or [{}])[0]


# NEW: diagnostic history reader.
# It does not save, delete or modify any VK messages.
def vk_get_history(peer_id, count=10):
    r = requests.get(
        "https://api.vk.com/method/messages.getHistory",
        params={
            "access_token": VK_TOKEN,
            "v": "5.199",
            "peer_id": peer_id,
            "count": count,
        },
        timeout=15,
    )
    r.raise_for_status()

    data = r.json()

    if data.get("error"):
        error = data["error"]
        raise RuntimeError(
            f"VK API [{error.get('error_code', '?')}]: "
            f"{error.get('error_msg', 'Unknown error')}"
        )

    response = data.get("response") or {}
    return response.get("items") or []


def text_after_bot_tag(text):
    gid = re.escape(VK_GROUP_ID)
    for p in [
        rf"^\s*\[club{gid}\|[^\]]+\]\s*",
        rf"^\s*@club{gid}\b[\s,:-]*",
    ]:
        m = re.match(p, text, re.I)
        if m:
            return text[m.end():].strip()
    return None


def mark_event(event_id, user_id, peer_id):
    google_post({
        "action": "markEventProcessed",
        "eventId": event_id,
        "vkId": str(user_id),
