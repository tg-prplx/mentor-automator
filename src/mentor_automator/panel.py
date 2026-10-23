import asyncio
import json
import secrets

from aiohttp import web

HTML = """<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Mentor · наблюдение</title><style>
:root{font:16px system-ui;color:#e8edf4;background:#101820}body{max-width:1000px;margin:40px auto;padding:0 24px}
header{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #34434b;padding-bottom:20px}
h1{font-size:28px;margin:0}small,label{color:#9aabb6}#state{color:#82e8bb;font:16px monospace}
section{padding:24px 0;border-bottom:1px solid #34434b}.stats{display:flex;gap:44px;flex-wrap:wrap}
strong{font-size:28px;display:block}#question{white-space:pre-wrap;line-height:1.7;font-size:19px;max-height:300px;overflow:auto}
button{background:#243741;color:#e8edf4;border:1px solid #4d6975;border-radius:7px;padding:11px 20px;margin:0 8px 8px 0;cursor:pointer}
button:hover{background:#355561}pre{white-space:pre-wrap;line-height:1.65;color:#bdd1dd}#explanation{line-height:1.6}
</style><header><div><h1>Mentor</h1><small>Локальное наблюдение · Codex / Moodle</small></div><span id="state">IDLE</span></header>
<section class="stats"><div><label>Заполнено</label><strong id="filled">0</strong></div><div><label>Пропущено</label><strong id="skipped">0</strong></div>
<div><label>Осталось времени</label><strong id="timer">—</strong></div><div><label>Уверенность</label><strong id="confidence">—</strong></div></section>
<section><label>Текущий вопрос</label><div id="question">Ожидание запуска через Telegram</div><p>Ответ: <span id="answer">—</span></p>
<p id="explanation"></p></section><section><button data-action="pause">Пауза</button><button data-action="resume">Продолжить</button>
<button data-action="stop">Остановить</button><button data-action="scan">Проверить курсы</button>
<p><small>Завершение попытки подтверждается отдельной кнопкой в Telegram. Пауза не останавливает таймер Moodle.</small></p><p id="result"></p></section>
<section><label>Мониторинг</label><p id="monitor"></p><label>Последние действия</label><pre id="actions"></pre></section>
<script>
const token=new URLSearchParams(location.search).get('token');
const stream=new EventSource('/events?token='+encodeURIComponent(token));
stream.onmessage=e=>{const s=JSON.parse(e.data);for(const k of ['state','question','filled','skipped','answer','explanation','timer','monitor'])
document.getElementById(k).textContent=s[k];document.getElementById('confidence').textContent=s.confidence===null?'—':Math.round(s.confidence*100)+'%';
document.getElementById('actions').textContent=s.actions.join('\\n');};
stream.onerror=()=>{document.getElementById('state').textContent='Связь с приложением потеряна';};
document.querySelectorAll('button').forEach(b=>b.onclick=async()=>{try{let r=await fetch('/control/'+b.dataset.action,{method:'POST',headers:{'X-Panel-Token':token}});
document.getElementById('result').textContent=r.ok?'Команда выполнена':'Команда недоступна в текущем состоянии';}catch{document.getElementById('result').textContent='Приложение недоступно';}});
</script></html>"""


class Panel:
    def __init__(self, events, agent, monitor):
        self.events, self.agent, self.monitor = events, agent, monitor
        self.token = secrets.token_urlsafe(32)
        self.app = web.Application(middlewares=[self.authorize])
        self.app.add_routes(
            [
                web.get("/", self.index),
                web.get("/events", self.stream),
                web.post("/control/{action}", self.control),
            ]
        )

    @web.middleware
    async def authorize(self, request, handler):
        if request.host != f"127.0.0.1:{self.port}":
            raise web.HTTPForbidden()
        token = (
            request.headers.get("X-Panel-Token", "")
            if request.method == "POST"
            else request.query.get("token", "")
        )
        if not secrets.compare_digest(token, self.token):
            raise web.HTTPForbidden()
        origin = request.headers.get("Origin")
        if origin and origin != f"http://127.0.0.1:{self.port}":
            raise web.HTTPForbidden()
        response = await handler(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    async def index(self, request):
        return web.Response(text=HTML, content_type="text/html")

    async def stream(self, request):
        response = web.StreamResponse(
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store"}
        )
        await response.prepare(request)
        queue = self.events.subscribe()
        try:
            while True:
                try:
                    snapshot = await asyncio.wait_for(queue.get(), 10)
                    payload = "data: " + json.dumps(snapshot, ensure_ascii=False) + "\n\n"
                except TimeoutError:
                    payload = ": keepalive\n\n"
                await response.write(payload.encode())
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.events.subscribers.discard(queue)
        return response

    async def control(self, request):
        try:
            action = request.match_info["action"]
            if action == "scan":
                self.monitor.scan_now.set()
            elif action in {"pause", "resume", "stop"}:
                await getattr(self.agent, action)()
            else:
                raise ValueError("Unknown command")
            return web.json_response({"ok": True})
        except Exception:
            return web.json_response({"ok": False}, status=409)

    async def open(self):
        self.runner = web.AppRunner(self.app, access_log=None, shutdown_timeout=1)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{self.port}/?token={self.token}"

    async def close(self):
        await self.runner.cleanup()
