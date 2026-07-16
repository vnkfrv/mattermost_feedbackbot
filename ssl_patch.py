import ssl
import asyncio
import logging
import websockets
import mattermostdriver.websocket as mm_websocket

log = logging.getLogger("ssl_patch")


async def patched_connect(self, event_handler):
    scheme = str(self.options.get("scheme", "")).lower()
    # TLS определяем по схеме (https/wss) или по порту 443.
    is_tls = scheme in ("https", "wss") or self.options.get("port") == 443

    if is_tls:
        context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
        context.check_hostname = False  # не проверяем hostname сертификата
        context.verify_mode = ssl.CERT_NONE  # не проверяем сам сертификат (корп CA)
        ws_scheme, ssl_ctx = "wss", context
    else:
        ws_scheme, ssl_ctx = "ws", None

    basepath = self.options.get("basepath", "/api/v4")
    url = (
        f"{ws_scheme}://{self.options['url']}:{self.options['port']}"
        f"{basepath}/websocket"
    )

    self._alive = True  # флаг что бот работает
    while self._alive:  # крутимся пока бот жив
        try:
            # ssl=None для ws:// (локально), ssl=context для wss:// (прод)
            async with websockets.connect(url, ssl=ssl_ctx) as websocket:
                await self._authenticate_websocket(websocket, event_handler)
                await self._start_loop(websocket, event_handler)
        except Exception as e:
            log.warning(
                "Websocket оборвался (%s: %s), переподключение через 5с",
                type(e).__name__,
                e,
            )
            await asyncio.sleep(5)  # ждём и переподключаемся


def sslapply():
    mm_websocket.Websocket.connect = patched_connect
