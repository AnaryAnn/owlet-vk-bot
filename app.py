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
DIGEST_SCRIPT_URL = os.environ.get("DIGEST_SCRIPT_URL", "").strip()
DIGEST_SCRIPT_PASSWORD = os.environ.get("DIGEST_SCRIPT_PASSWORD", "").strip()

PAIR_RE = re.compile(r"^\s*(\d+)\s*,\s*(\d+)\s*$")

def google_post(payload):
    payload = dict(payload)
    payload["password"] = GOOGLE_SCRIPT_PASSWORD
    r = requests.post(GOOGLE_SCRIPT_URL, json=payload, timeout=15)
    r.raise_for_status()
    return r.json()

def digest_post(payload):
    if not DIGEST_SCRIPT_URL or not DIGEST_SCRIPT_PASSWORD:
        raise RuntimeError("Digest storage is not configured")
    payload = dict(payload)
    payload["password"] = DIGEST_SCRIPT_PASSWORD
    r = requests.post(DIGEST_SCRIPT_URL, json=payload, timeout=15)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(data.get("error", "Digest Script error"))
    return data

def vk_send(peer_id, text):
    r = requests.post("https://api.vk.com/method/messages.send", data={
        "access_token": VK_TOKEN, "v": "5.199", "peer_id": peer_id,
        "random_id": random.randint(1, 2147483647), "message": text
    }, timeout=10)
    r.raise_for_status()

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
    })

def mark_event(event_id, user_id, peer_id):
    return google_post({
        "action":"markEventProcessed", "eventId":event_id,
        "vkId":str(user_id), "peerId":str(peer_id)
    })

@app.get("/")
def health():
    return {"ok":True, "service":"sychnaya-ohota-v6.5-digest-buffer"}

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

        try:
            save_chat_message(msg, event_id)
        except Exception as e:
            print("DIGEST SAVE ERROR:", repr(e))

        payload = text_after_bot_tag(text)
        if payload is None:
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
