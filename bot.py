import os
import re
import sys
import json
import signal
import asyncio
import logging

import ssl_patch
from mattermostdriver.websocket import Websocket

from idea_dialog import (
    driver,
    run_in_thread,
    greeting_attachment,
    start_server,
    _build_body,
    now_str,
)
from send_email import send_email

log = logging.getLogger("bot")
BOT_USERNAME = os.getenv("BOT_USERNAME")


FEEDBACK_QUESTIONS = [
    "Что не нравится?",
    "Как это можно исправить?",
    "Какие плюсы это даст?",
]
FEEDBACK_TAIL = (
    " Если хотите отправить еще одно предложение, просто напишите `feedback`"
)
FEEDBACK_FAIL = "Идея записана, но письмо не ушло — сообщите администратору."

_dialogs = {}

_MENTION_RE = (
    re.compile(rf"@{re.escape(BOT_USERNAME)}\b", re.IGNORECASE)
    if BOT_USERNAME
    else None
)
if _MENTION_RE is None:
    log.warning("BOT_USERNAME не задан в .env — бот не будет реагировать на упоминания")


async def _post(channel_id, text):
    """Отправить обычное текстовое сообщение в канал/ЛС."""
    await run_in_thread(
        driver.posts.create_post, {"channel_id": channel_id, "message": text}
    )


async def _handle_feedback_dialog(user_id, channel_id, text):
    """Шаг текстового опроса. Возвращает True, если сообщение обработано опросом."""
    key = (user_id, channel_id)

    # запуск или перезапуск
    if text.lower() == "feedback":
        _dialogs[key] = {"step": 0, "answers": []}
        await _post(channel_id, FEEDBACK_QUESTIONS[0])
        return True

    # отсеивать лишнее
    state = _dialogs.get(key)
    if state is None:
        return False

    # ответы
    state["answers"].append(text)
    state["step"] += 1
    if state["step"] < len(FEEDBACK_QUESTIONS):
        await _post(channel_id, FEEDBACK_QUESTIONS[state["step"]])
        return True

    # письмо +итог сообщение
    _dialogs.pop(key, None)
    ok = await run_in_thread(
        send_email, "Новая идея от сотрудника", _build_body(*state["answers"])
    )
    thanks = (
        f"Спасибо! Идея отправлена ({now_str()}), специалист изучит Ваше "
        f"предложение.{FEEDBACK_TAIL}"
    )
    await _post(channel_id, thanks if ok else FEEDBACK_FAIL)
    return True


async def handle_message(raw):
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw

        if data.get("event") != "posted":
            return

        post = json.loads(data["data"]["post"])
        sender = data["data"].get("sender_name", "")
        message = (post.get("message") or "").strip()
        channel_id = post.get("channel_id")
        user_id = post.get("user_id")

        if sender in (BOT_USERNAME, f"@{BOT_USERNAME}"):
            return

        if message.lower() == "check":
            log.info("check от user_id=%s channel_id=%s", user_id, channel_id)
            await _post(channel_id, "Бот отвечает")
            return

        if await _handle_feedback_dialog(user_id, channel_id, message):
            return

        if _MENTION_RE and _MENTION_RE.search(message):
            await run_in_thread(
                driver.posts.create_post,
                {
                    "channel_id": channel_id,
                    "message": "",
                    "props": {"attachments": greeting_attachment()},
                },
            )
    except Exception as e:
        log.exception("Ошибка обработки сообщения: %s", e)


async def main():
    ssl_patch.sslapply()

    try:
        await run_in_thread(driver.login)
    except Exception as e:
        log.critical("Не удалось подключиться к Mattermost: %s", e)
        sys.exit(1)

    try:
        runner = await start_server()
    except OSError as e:
        log.critical("Не удалось поднять HTTP-сервер (порт занят?): %s", e)
        sys.exit(1)
    log.info("@%s запущен", BOT_USERNAME)

    driver.websocket = Websocket(driver.options, driver.client.token)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    ws_task = asyncio.create_task(driver.websocket.connect(handle_message))
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        await asyncio.wait({ws_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        try:
            driver.websocket.disconnect()
        except Exception:
            pass
        ws_task.cancel()
        try:
            await ws_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.warning("Ошибка при остановке websocket: %s", e)
        try:
            await runner.cleanup()
        except Exception as e:
            log.warning("Ошибка при остановке HTTP-сервера: %s", e)
        log.info("Бот остановлен")


if __name__ == "__main__":
    asyncio.run(main())
