        "peerId": str(peer_id),
    })


@app.get("/")
def health():
    return {"ok": True, "service": "sychnaya-ohota-v6.4-history-test"}


@app.post("/vk")
def vk_callback():
    data = request.get_json(silent=True) or {}

    if data.get("type") == "confirmation":
        return Response(VK_CONFIRMATION_CODE, mimetype="text/plain")

    if data.get("type") != "message_new":
        return Response("ok")

    if str(data.get("group_id", "")) != VK_GROUP_ID:
        return Response("ok")

    msg = ((data.get("object") or {}).get("message") or {})
    user_id = msg.get("from_id")
    peer_id = msg.get("peer_id")
    text = (msg.get("text") or "").strip()
    event_id = data.get("event_id")

    if not user_id or not peer_id or int(peer_id) < 2000000000:
        return Response("ok")

    check = google_post({
        "action": "checkEvent",
        "eventId": event_id,
        "vkId": str(user_id),
        "peerId": str(peer_id),
    })

    if check.get("exists"):
        return Response("ok")

    payload = text_after_bot_tag(text)
    if payload is None:
        return Response("ok")

    # NEW: intercept the diagnostic command before PAIR_RE.
    # The existing plan/fact branch below is unchanged.
    if payload.strip().lower() == "тест истории":
        try:
            messages = vk_get_history(peer_id, count=10)

            if messages:
                vk_send(
                    peer_id,
                    "🦉 История беседы доступна!\n\n"
                    f"VK вернул сообщений: {len(messages)}\n"
                    "Тест пройден. Можно подключать Сычевестник 🤖"
                )
            else:
                vk_send(
                    peer_id,
                    "🦉 VK разрешил запрос истории, "
                    "но не вернул ни одного сообщения."
                )

        except Exception as e:
            print("HISTORY TEST ERROR:", repr(e))
            try:
                vk_send(
                    peer_id,
                    "🦉 Тест истории не пройден.\n\n"
                    f"{e}"
                )
            except Exception as send_error:
                print("HISTORY TEST SEND ERROR:", repr(send_error))

        # IMPORTANT: VK Callback always receives "ok".
        return Response("ok")

    m = PAIR_RE.match(payload)

    if not m:
        vk_send(peer_id, "🦉 Формат: план, факт\nНапример: 30, 17")
        return Response("ok")

    plan, fact = map(int, m.groups())

    try:
        result = google_post({
            "action": "vkSave",
            "vkId": str(user_id),
            "plan": plan,
            "fact": fact,
        })

        if result.get("unknownVkUser"):
            profile = vk_get_user(user_id)
            bind = google_post({
                "action": "vkAutoBind",
                "vkId": str(user_id),
                "firstName": profile.get("first_name", ""),
                "lastName": profile.get("last_name", ""),
            })

            if bind.get("success"):
                result = google_post({
                    "action": "vkSave",
                    "vkId": str(user_id),
                    "plan": plan,
                    "fact": fact,
                })

        if result.get("success"):
            mark_event(event_id, user_id, peer_id)
            vk_send(
                peer_id,
                f"🦉 Данные сохранены!\n"
                f"{result.get('name','')}: план {plan} 🐭, факт {fact} 🐭"
            )
        else:
            vk_send(peer_id, "Не удалось сохранить данные.")

    except Exception as e:
        print("ERROR:", repr(e))
        try:
            vk_send(peer_id, "Не удалось сохранить данные.")
        except Exception:
            pass
