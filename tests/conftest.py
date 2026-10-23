import json
from pathlib import Path
from types import SimpleNamespace

import pytest_asyncio
from aiohttp import web

from mentor_automator.browser import Browser
from mentor_automator.config import Selectors, Settings
from mentor_automator.storage import Store

PROJECT = Path(__file__).resolve().parents[1]
SENTINEL = "SESSION_SENTINEL_NEVER_EXPORT"


def shell(content):
    return f"<html><body><nav class='usermenu'>{SENTINEL}</nav><main>{content}</main></body></html>"


@pytest_asyncio.fixture
async def site():
    state = SimpleNamespace(
        submissions=0,
        starts=0,
        saves=[],
        available=True,
        answered={},
        login_count=0,
        start_mode="direct",
        active_attempt=False,
        render_delay=0,
        form_enctype="application/x-www-form-urlencoded",
        close_on_next=False,
        closed=False,
        custom_questions=None,
        final_mode="direct",
        summary_delay=0,
        summary_visits=0,
        ajax_calls=[],
    )

    async def my(request):
        if not request.cookies.get("demo-session"):
            raise web.HTTPFound("/login/index.php")
        return web.Response(
            text=shell('<a href="/course/view.php?id=5">Физика</a>'), content_type="text/html"
        )

    async def login(request):
        if request.method == "POST":
            form = await request.post()
            assert form["username"] == "demo_student" and form["password"] == "fictional=pass"
            state.login_count += 1
            response = web.HTTPFound("/my/")
            response.set_cookie("demo-session", "fictional-local-only")
            raise response
        return web.Response(
            text="""<form method="post"><input id="username" name="username">
          <input id="password" name="password" type="password"><button id="loginbtn">Войти</button></form>""",
            content_type="text/html",
        )

    async def course(request):
        return web.Response(
            text=shell('<a href="/mod/quiz/view.php?id=7&sesskey=SECRET_SENTINEL">Механика</a>'),
            content_type="text/html",
        )

    async def quiz(request):
        action = (
            '<form action="/mod/quiz/startattempt.php" method="post"><button>Attempt quiz</button></form>'
            if state.available
            else "No more attempts"
        )
        if state.start_mode == "mentor" and state.available:
            action = """<form action="/mod/quiz/startattempt.php" method="post">
              <button id="single_button" type="submit">Спроба тесту</button></form>
              <div class="mod_quiz_preflight_popup" style="display:none" id="preflight">
              <form action="/mod/quiz/startattempt.php" method="post">
              <input type="submit" id="id_submitbutton" name="submitbutton" value="Почати спробу">
              <input type="submit" id="id_cancel" name="cancel" value="Скасувати"></form></div>
              <script>document.querySelector('#single_button').onclick=e=>{
                e.preventDefault();setTimeout(()=>document.querySelector('#preflight').style.display='block',800)
              }</script>"""
        if state.start_mode == "popup" and state.available:
            action = """<form action="/mod/quiz/startattempt.php" method="post">
              <button type="submit" onclick="event.preventDefault();window.open('/mod/quiz/startattempt.php','quizpopup')">Спроба тесту</button></form>"""
        if state.active_attempt and not state.submissions:
            action = '<a href="/mod/quiz/attempt.php?attempt=10&cmid=7&page=0">Продовжити спробу</a>'
        return web.Response(
            text=shell(
                '<div class="quizinfo">Closes: 30 October 2026\nTime limit: 20 minutes\nAttempts remaining: 2</div>'
                + action
            ),
            content_type="text/html",
        )

    async def start(request):
        state.starts += 1
        state.active_attempt = True
        raise web.HTTPFound("/mod/quiz/attempt.php?attempt=10&cmid=7&page=0")

    def choice(qid, kind, question, labels):
        controls = "".join(
            f'<div><input id="{qid}_{i}" type="{kind}" name="{qid}" value="{i}"><label for="{qid}_{i}">{label}</label></div>'
            for i, label in enumerate(labels)
        )
        return f'<div class="que" id="{qid}"><div class="content"><div class="qtext">{question}</div><div class="answer">{controls}</div></div></div>'

    async def attempt(request):
        if state.submissions or state.closed:
            raise web.HTTPFound("/mod/quiz/review.php?attempt=10")
        page = int(request.query.get("page", 0))
        if page == 0:
            questions = choice(
                "q1",
                "radio",
                '2+2? <math><semantics><mi>x</mi><annotation encoding="application/x-tex">x^2</annotation></semantics></math>',
                ["3", "4"],
            )
            questions += choice("q2", "checkbox", "Выберите чётные числа", ["2", "3", "4"])
            next_button = '<button name="next" value="Next page">Next page</button>'
        elif page == 1:
            questions = '<div class="que" id="q3"><div class="content"><div class="qtext">Название спутника Земли<table><tr><td>Земля</td><td>?</td></tr></table></div><div class="answer"><input name="q3" type="text"></div></div></div>'
            questions += '<div class="que numerical" id="q4"><div class="content"><div class="qtext">Половина пяти</div><div class="answer"><input name="q4" type="text"></div></div></div>'
            next_button = '<button name="next" value="Next page">Next page</button>'
        else:
            questions = '<div class="que" id="q5"><div class="content"><div class="qtext">Сопоставьте вручную</div><div class="answer"><div data-custom-widget="unsupported">A</div></div></div></div>'
            next_button = '<button name="next" value="Finish attempt">Finish attempt</button>'
        if page == 0 and state.custom_questions:
            questions = state.custom_questions
        if state.render_delay:
            questions = (
                '<div id="delayed-questions"></div><script>setTimeout(()=>'
                'document.querySelector("#delayed-questions").innerHTML='
                + json.dumps(questions)
                + f",{state.render_delay})</script>"
            )
        nextpage = page + 1 if page < 2 else -1
        content = f'''<span id="quiz-time-left">04:59</span><form id="responseform" method="post" enctype="{state.form_enctype}" action="/mod/quiz/processattempt.php">
          <input type="hidden" name="attempt" value="10"><input type="hidden" name="page" value="{page}">
          <input type="hidden" name="nextpage" value="{nextpage}"><input type="hidden" name="timeup" value="0">
          <input type="hidden" name="sesskey" value="{SENTINEL}">{questions}{next_button}</form>
          <a class="endtestlink" href="/mod/quiz/summary.php?attempt=10">Finish attempt</a>'''
        return web.Response(text=shell(content), content_type="text/html")

    async def process(request):
        form = dict(await request.post())
        if state.close_on_next:
            state.closed = True
            raise web.HTTPFound("/mod/quiz/review.php?attempt=10")
        if form.get("finishattempt") == "1":
            state.submissions += 1
            raise web.HTTPFound("/mod/quiz/review.php?attempt=10")
        state.saves.append({k: v for k, v in form.items() if k.startswith("q")})
        if form.get("nextpage") == "-1":
            raise web.HTTPFound("/mod/quiz/summary.php?attempt=10")
        raise web.HTTPFound(f"/mod/quiz/attempt.php?attempt=10&cmid=7&page={int(form.get('page', 0)) + 1}")

    async def summary(request):
        state.summary_visits += 1
        content = """<form id="final-form" method="post" action="/mod/quiz/processattempt.php">
          <input type="hidden" name="attempt" value="10"><input type="hidden" name="finishattempt" value="1">
          <button>Submit all and finish</button></form>"""
        if state.final_mode in {"uk_delayed", "bootstrap"}:
            content = content.replace("Submit all and finish", "Надіслати все і завершити")
            content += """<script>
            document.querySelector('#final-form').onsubmit=e=>{
              e.preventDefault();setTimeout(()=>{
                const dialog=document.createElement('div');
                dialog.className='moodle-dialogue-base confirmation-dialogue';
                dialog.innerHTML='<div class="confirmation-buttons"><input type="button" class="btn btn-primary" value="Так, надіслати"><button type="button">Скасувати</button></div>';
                dialog.querySelector('input').onclick=()=>document.querySelector('#final-form').submit();
                document.body.append(dialog);
              },900);
            };</script>"""
        if state.final_mode == "bootstrap":
            content += """<script>
            const ajax=methodname=>fetch('/lib/ajax/service.php',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify([{methodname,args:{}}])});
            const bootstrap=Promise.all([ajax('core_output_load_template_with_dependencies'),ajax('core_get_strings')]);
            ajax('blocked_analytics_write').catch(()=>{});
            const original=document.querySelector('#final-form').onsubmit;
            document.querySelector('#final-form').onsubmit=async e=>{e.preventDefault();await bootstrap;original(e)};
            </script>"""
        if state.final_mode == "native":
            content = content.replace(
                '<form id="final-form"',
                '<form onsubmit="return confirm(\'Надіслати всі відповіді?\')" id="final-form"',
            )
        if state.summary_delay:
            content = (
                '<div id="summary-content"></div><script>setTimeout(()=>{document.querySelector("#summary-content").innerHTML='
                + json.dumps(content)
                + f"}},{state.summary_delay})</script>"
            )
        return web.Response(text=shell(content), content_type="text/html")

    async def review(request):
        return web.Response(
            text=shell('<div data-attempt-submitted="true">Завершено</div>'), content_type="text/html"
        )

    async def ajax(request):
        items = await request.json()
        state.ajax_calls.extend(item["methodname"] for item in items)
        return web.json_response([{"error": False, "data": {}} for _ in items])

    app = web.Application()
    app.add_routes(
        [
            web.get("/", my),
            web.get("/my/", my),
            web.get("/my/courses.php", my),
            web.route("*", "/login/index.php", login),
            web.get("/course/view.php", course),
            web.get("/mod/quiz/index.php", course),
            web.get("/mod/quiz/view.php", quiz),
            web.post("/mod/quiz/startattempt.php", start),
            web.get("/mod/quiz/startattempt.php", start),
            web.get("/mod/quiz/attempt.php", attempt),
            web.post("/mod/quiz/processattempt.php", process),
            web.get("/mod/quiz/summary.php", summary),
            web.get("/mod/quiz/review.php", review),
            web.post("/lib/ajax/service.php", ajax),
        ]
    )
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    server = web.TCPSite(runner, "127.0.0.1", 0)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", state
    await runner.cleanup()


@pytest_asyncio.fixture
async def environment(tmp_path, site):
    url, state = site
    settings = Settings(
        root=tmp_path,
        moodle_url=url,
        selectors_file=PROJECT / "config/selectors.json",
        action_delay_min=0,
        action_delay_max=0,
        llm_retries=0,
        uncertain_answer_policy="skip",
        browser_channel="chromium",
    )
    selectors = Selectors(settings.selectors_file)
    browser = Browser(settings, selectors)
    await browser.open(headless=True)
    store = Store(tmp_path / "state.sqlite3")
    await store.open()
    yield settings, selectors, browser, store, state
    await browser.close()
    await store.close()
