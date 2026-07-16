import os
import sys
import hmac
import time
import asyncio
import logging

import urllib3
from dotenv import load_dotenv
from aiohttp import web
from mattermostdriver import Driver

from send_email import send_email

# ошибка по ssl
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
BOT_USERNAME = os.getenv("BOT_USERNAME")
log = logging.getLogger(BOT_USERNAME or "feedback_bot")


def _env_int(name, default):
    """int из env с понятной ошибкой вместо голого ValueError при импорте."""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        log.critical("%s должно быть числом, получено: %r", name, raw)
        sys.exit(1)



BOT_PUBLIC_URL = os.getenv("BOT_PUBLIC_URL", "")
BUTTON_PORT = _env_int("BUTTON_PORT", 8080)


BUTTON_SECRET = os.getenv("BUTTON_SECRET", "")

# не чаще чем
_RATE_LIMIT_SECONDS = 30
_last_submit = {}  

# Удерживаем ссылки на фоновые задачи, иначе GC может убить их до завершения.
_bg_tasks = set()


try:
    driver = Driver(
        {
            "url": os.getenv("URL"),
            "token": os.getenv("BOT_TOKEN"),
            "scheme": os.getenv("SCHEME", "https"),
            "port": _env_int("PORT", 443),
            "verify": False,  
        }
    )
except Exception as e:
    log.critical("Не удалось инициализировать клиент Mattermost: %s", e)
    sys.exit(1)


async def run_in_thread(func, *args, **kwargs):
    """Синхронный вызов драйвера — в отдельном потоке, чтобы не блокировать loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))



def _cb_url(path):
    """URL callback'а с секретом (если задан)."""
    url = f"{BOT_PUBLIC_URL}{path}"
    if BUTTON_SECRET:
        url += f"?secret={BUTTON_SECRET}"
    return url


def _check_secret(request):
    """True, если запрос можно обслуживать. Пустой секрет = открыто (отладка).
    compare_digest — сравнение за постоянное время (защита от timing-атак)."""
    if not BUTTON_SECRET:
        return True
    supplied = request.query.get("secret") or ""
    return hmac.compare_digest(supplied, BUTTON_SECRET)


def _rate_limited(user_id):
    """True, если пользователь отправлял идею слишком недавно."""
    now = time.monotonic()
    if now - _last_submit.get(user_id, 0.0) < _RATE_LIMIT_SECONDS:
        return True
    _last_submit[user_id] = now
    return False



WELCOME_TEXT = "Привет! Этот FeedBack-бот создан, чтобы ты мог предложить @drobot_n идею по улучшению работы в компании. Все сообщения отправляются анонимно. Тебе нужно заполнить небольшую форму: описать проблему, предложить своё видение решения и рассказать, какие плюсы это принесёт. Приступим?"


def _open_button(label):
    """Кнопка, открывающая модалку. context.action по нему опознаём клик."""
    return {
        "id": "ideaopen",  
        "name": label,
        "type": "button",
        "integration": {
            "url": _cb_url("/button"),
            "context": {"action": "idea_open"},
        },
    }


def greeting_attachment(label="Да, приступим", text=WELCOME_TEXT):

    return [{"text": text, "actions": [_open_button(label)]}]


def _idea_dialog(post_id):
    return {
        "callback_id": "idea",
        "title": "Предложить идею",
        "submit_label": "Отправить",
        "state": post_id or "",
        "elements": [
            {
                "display_name": "Что не нравится?",
                "name": "first",
                "type": "textarea",
                "max_length": 3000,
                "help_text": "Опишите проблему",
            },
            {
                "display_name": "Как это можно исправить?",
                "name": "second",
                "type": "textarea",
                "max_length": 3000,
                "optional": True,
                "help_text": "Ваше предложение (необязательно)",
            },
            {
                "display_name": "Какие плюсы это даст?",
                "name": "third",
                "type": "textarea",
                "max_length": 3000,
                "optional": True,
                "help_text": "Ожидаемый эффект (необязательно)",
            },
        ],
    }


async def _open_dialog(trigger_id, dialog):
    if not trigger_id:
        log.error("open_dialog: пустой trigger_id — Mattermost не прислал его")
        return
    try:
        await run_in_thread(
            driver.integration_actions.open_dialog,
            {
                "trigger_id": trigger_id,
                "url": _cb_url("/dialog"),
                "dialog": dialog,
            },
        )
    except Exception as e:
        log.exception("open_dialog не удался (trigger_id=%s): %s", trigger_id, e)


async def _handle_button(request):

    if not _check_secret(request):
        log.warning("Отклонён /button с неверным секретом от %s", request.remote)
        return web.json_response({}, status=403)
    try:
        try:
            data = await request.json()
        except Exception:
            data = {}

        if (data.get("context") or {}).get("action") != "idea_open":
            return web.json_response({})

        post_id = data.get("post_id")  # пост, на котором нажали кнопку
        log.info("Открываю модалку, post_id=%s", post_id)
        await _open_dialog(data.get("trigger_id"), _idea_dialog(post_id))
    except Exception as e:
        log.exception("Ошибка обработки /button: %s", e)
    return web.json_response({})


async def _handle_dialog(request):

    if not _check_secret(request):
        log.warning("Отклонён /dialog с неверным секретом от %s", request.remote)
        return web.json_response({}, status=403)
    try:
        try:
            data = await request.json()
        except Exception:
            data = {}

        if data.get("cancelled") or data.get("callback_id") != "idea":
            return web.json_response({})

        submission = data.get("submission") or {}
        post_id = data.get("state") or ""  # вернулся из dialog["state"]
        user_id = data.get("user_id")

        first = (submission.get("first") or "").strip()
        second = (submission.get("second") or "").strip()
        third = (submission.get("third") or "").strip()

        if not first:
            return web.json_response({"errors": {"first": "Опишите, что не нравится"}})

        if _rate_limited(user_id):
            return web.json_response(
                {"errors": {"first": "Вы недавно отправляли идею — подождите немного."}}
            )

        task = asyncio.create_task(_finish(first, second, third, post_id))
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
    except Exception as e:
        log.exception("Ошибка обработки /dialog: %s", e)
    return web.json_response({})


async def _finish(first, second, third, post_id):
    try:
        body = _build_body(first, second, third)
        ok = await run_in_thread(send_email, "Новая идея от сотрудника", body)
        final_text = (
            "Спасибо! Идея отправлена, специалист изучит Ваше предложение."
            if ok
            else "Идея записана, но письмо не ушло — сообщите администратору."
        )
        if post_id:
            await run_in_thread(
                driver.posts.patch_post,
                post_id,
                {
                    "props": {
                        "attachments": greeting_attachment(
                            "Предложить ещё одну идею", final_text
                        )
                    }
                },
            )
    except Exception as e:
        log.exception("Ошибка завершения отправки идеи: %s", e)


def _build_body(first, second, third):
    return (
        f"Что не нравится:\n{first or '—'}\n\n"
        f"Как исправить:\n{second or '—'}\n\n"
        f"Какие плюсы:\n{third or '—'}\n"
    )


async def start_server():
    if not BOT_PUBLIC_URL:
        log.warning("BOT_PUBLIC_URL пуст — Mattermost не сможет прислать клики/submit")
    if not BUTTON_SECRET:
        log.warning(
            "BUTTON_SECRET не задан — эндпоинты /button и /dialog открыты. "
            "Для прод-развёртывания обязательно задайте секрет в .env!"
        )

    app = web.Application()
    app.router.add_post("/button", _handle_button)
    app.router.add_post("/dialog", _handle_dialog)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", BUTTON_PORT)
    await site.start()
    log.info("HTTP-сервер слушает на 0.0.0.0:%d", BUTTON_PORT)
    return runner
