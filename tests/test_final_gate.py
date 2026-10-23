import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from mentor_automator.browser import FinalGate


def route(path, body="", content_type="application/x-www-form-urlencoded", method="POST"):
    request = SimpleNamespace(
        url="https://mentor.khai.edu" + path,
        post_data=body,
        post_data_buffer=body.encode(),
        method=method,
        headers={"content-type": content_type},
    )
    return SimpleNamespace(request=request, abort=AsyncMock(), continue_=AsyncMock())


async def test_json_multipart_and_summary_navigation_fail_closed():
    gate = FinalGate()
    cases = [
        route(
            "/mod/quiz/processattempt.php",
            json.dumps({"attempt": 10, "finishattempt": 1}),
            "application/json",
        ),
        route("/mod/quiz/processattempt.php", "opaque", "multipart/form-data; boundary=fake"),
        route("/mod/quiz/processattempt.php", "attempt=10&next=1&nextpage=-1"),
        route("/mod/quiz/summary.php?attempt=10", method="GET"),
        route("/mod/quiz/unknown.php", "attempt=10"),
    ]
    for item in cases:
        await gate.route(item)
        assert item.abort.await_count == 1 and item.continue_.await_count == 0


async def test_one_final_write_with_multiple_ui_steps():
    gate = FinalGate()
    gate.arm("10")
    summary = route("/mod/quiz/summary.php?attempt=10", method="GET")
    await gate.route(summary)
    assert summary.continue_.await_count == 1
    wrong = route("/mod/quiz/processattempt.php", "attempt=11&finishattempt=1")
    await gate.route(wrong)
    assert wrong.abort.await_count == 1
    first = route("/mod/quiz/processattempt.php", "attempt=10&finishattempt=1")
    await gate.route(first)
    assert first.continue_.await_count == 1
    replay = route("/mod/quiz/processattempt.php", "attempt=10&finishattempt=1")
    await gate.route(replay)
    assert replay.abort.await_count == 1
    replay_get = route("/mod/quiz/processattempt.php?attempt=10&finishattempt=1", method="GET")
    await gate.route(replay_get)
    assert replay_get.abort.await_count == 1


async def test_ajax_discovery_allowed_unknown_service_denied():
    gate = FinalGate()
    gate.active_attempt = "10"
    readonly = route(
        "/lib/ajax/service.php",
        json.dumps(
            [{"methodname": "core_course_get_enrolled_courses_by_timeline_classification", "args": {}}]
        ),
        "application/json",
    )
    await gate.route(readonly)
    assert readonly.continue_.await_count == 1
    final = route(
        "/lib/ajax/service.php",
        json.dumps(
            [{"methodname": "mod_quiz_process_attempt", "args": {"attemptid": 10, "finishattempt": True}}]
        ),
        "application/json",
    )
    await gate.route(final)
    assert final.abort.await_count == 1


def multipart(fields):
    boundary = "mentor-local-test"
    body = (
        "".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
            for name, value in fields
        )
        + f"--{boundary}--\r\n"
    )
    return body, f"multipart/form-data; boundary={boundary}"


async def test_multipart_save_allowed_final_flags_and_ambiguity_denied():
    gate = FinalGate()
    gate.active_attempt = "10"
    body, content_type = multipart(
        [("attempt", "10"), ("timeup", "0"), ("nextpage", "1"), ("next", "Далі"), ("q1", "answer")]
    )
    saving = route("/mod/quiz/processattempt.php", body, content_type)
    await gate.route(saving)
    assert saving.continue_.await_count == 1 and saving.abort.await_count == 0
    for extra in [
        [("finishattempt", "1")],
        [("timeup", "1")],
        [("nextpage", "-1")],
        [("attempt", "11")],
    ]:
        payload, content_type = multipart(
            [("attempt", "10"), ("timeup", "0"), ("nextpage", "1"), ("next", "Next page")] + extra
        )
        final = route("/mod/quiz/processattempt.php", payload, content_type)
        await gate.route(final)
        assert final.abort.await_count == 1


async def test_duplicate_query_and_body_flags_do_not_bypass_gate():
    gate = FinalGate()
    final = route(
        "/mod/quiz/processattempt.php?attempt=10&finishattempt=0",
        "attempt=10&finishattempt=1",
    )
    await gate.route(final)
    assert final.abort.await_count == 1
    gate.arm("10")
    ambiguous = route("/mod/quiz/processattempt.php?attempt=10", "attempt=11&finishattempt=1")
    await gate.route(ambiguous)
    assert ambiguous.abort.await_count == 1
