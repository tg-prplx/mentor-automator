import asyncio
import json
from pathlib import Path

import pytest
from question_fixtures import TRIANGLE, advanced_questions, marker_question
from test_browser_agent import FakeLLM, setup_agent

from mentor_automator.models import Answer, Assignment, Placement, State
from mentor_automator.moodle import StructureError


class AdvancedLLM(FakeLLM):
    async def solve(self, question, image=None):
        if question.id not in {"qm", "qe", "qo", "qw", "qd", "qi"}:
            return await super().solve(question, image)
        self.payloads.append(json.dumps(question.payload(), ensure_ascii=False))
        data = dict(choices=[], text="", explanation="Синтетический ответ", confidence=0.99, skip=False)
        mapping = {
            "qm": [
                Assignment(slot="0", choices=["2"], text=""),
                Assignment(slot="1", choices=["1"], text=""),
            ],
            "qe": [Assignment(slot="0", choices=[], text="4"), Assignment(slot="1", choices=["2"], text="")],
            "qw": [
                Assignment(slot="0", choices=["1"], text=""),
                Assignment(slot="1", choices=["0"], text=""),
            ],
            "qd": [
                Assignment(slot="0", choices=["1"], text=""),
                Assignment(slot="1", choices=["0"], text=""),
            ],
        }
        if question.id in mapping:
            data["assignments"] = mapping[question.id]
        elif question.id == "qo":
            data["order"] = ["2", "0", "1"]
        else:
            data["choices"] = ["1"]
        if question.visual:
            assert image and image.startswith(b"\x89PNG")
        return Answer(**data)


async def advanced_agent(environment, tmp_path):
    state = environment[-1]
    state.custom_questions = advanced_questions()
    state.form_enctype = "multipart/form-data"
    agent, _, quiz = await setup_agent(environment, tmp_path)
    llm = AdvancedLLM()
    agent.llm = llm
    return agent, llm, quiz


@pytest.mark.parametrize("question_id", ["qm", "qe", "qo", "qw", "qd", "qi"])
async def test_each_advanced_question_ui(environment, tmp_path, question_id):
    agent, llm, quiz = await advanced_agent(environment, tmp_path)
    await agent.moodle.start(quiz)
    q, index = next((q, i) for q, i in await agent.moodle.questions() if q.id == question_id)
    image = await agent.moodle.image(index) if q.visual else None
    answer = await llm.solve(q, image)
    await agent.moodle.apply(q, index, answer)


async def test_advanced_types_saved_and_final_confirmation_preserved(environment, tmp_path):
    agent, llm, quiz = await advanced_agent(environment, tmp_path)
    rid = await agent.start(quiz.id)
    await asyncio.wait_for(agent.task, 70)
    row = await agent.store.run(rid)
    assert row["state"] == State.AWAITING_FINAL_CONFIRMATION, row["error_code"]
    assert await agent.store.counts(rid) == (8, 1)
    assert environment[-1].submissions == 0
    saved = environment[-1].saves[0]
    assert saved["qm_0"] == "opaque-paris" and saved["qm_1"] == "opaque-berlin"
    assert saved["qe_0"] == "4" and saved["qe_1"] == "opaque-moon"
    assert saved["qo_response"] == "opaque-a,opaque-b,opaque-c"
    assert (saved["qw_1"], saved["qw_2"]) == ("2", "1")
    assert (saved["qd_1"], saved["qd_2"]) == ("2", "1")
    assert saved["qi"] == "opaque-triangle"
    for payload in llm.payloads:
        assert "opaque-" not in payload and "SESSION_SENTINEL" not in payload


async def test_invalid_slot_answers_and_duplicate_drag_rejected_before_actions(environment, tmp_path):
    agent, llm, quiz = await advanced_agent(environment, tmp_path)
    await agent.moodle.start(quiz)
    questions = dict((q.id, (q, i)) for q, i in await agent.moodle.questions())
    q, i = questions["qw"]
    invalid = Answer(
        choices=[],
        text="",
        explanation="test",
        confidence=0.99,
        skip=False,
        assignments=[
            Assignment(slot="0", choices=["1"], text=""),
            Assignment(slot="1", choices=["1"], text=""),
        ],
    )
    with pytest.raises(ValueError):
        await agent.moodle.apply(q, i, invalid)
    assert await agent.browser.page.locator("#qw input.placeinput").first.input_value() == "0"
    matching, _ = questions["qm"]
    invalid.assignments = [
        Assignment(slot="0", choices=["missing"], text=""),
        Assignment(slot="1", choices=["1"], text=""),
    ]
    with pytest.raises(ValueError):
        invalid.validate_for(matching)


async def test_unknown_widget_pauses_until_explicit_resume(environment, tmp_path):
    agent, _, quiz = await setup_agent(environment, tmp_path)
    agent.s.uncertain_answer_policy = "pause"
    notified = asyncio.Event()

    async def manual(rid, question):
        assert question.kind == "unsupported"
        notified.set()

    agent.notify_manual = manual
    rid = await agent.start(quiz.id)
    await asyncio.wait_for(notified.wait(), 25)
    assert (await agent.store.run(rid))["state"] == State.PAUSED
    assert agent.busy() and not agent.ready.is_set()
    assert environment[-1].submissions == 0
    await agent.resume()
    await asyncio.wait_for(agent.task, 10)
    assert (await agent.store.run(rid))["state"] == State.AWAITING_FINAL_CONFIRMATION


async def test_legacy_ordering_mouse_drag_and_response_field(environment, tmp_path):
    agent, llm, quiz = await advanced_agent(environment, tmp_path)
    await agent.moodle.start(quiz)
    await agent.browser.page.locator("#qo button").evaluate_all("nodes=>nodes.forEach(n=>n.remove())")
    question, index = next((q, i) for q, i in await agent.moodle.questions() if q.id == "qo")
    await agent.moodle.apply(question, index, await llm.solve(question))
    assert (
        await agent.browser.page.locator("[name=qo_response]").input_value() == "opaque-a,opaque-b,opaque-c"
    )


async def test_visual_crop_labels_cleaned_and_changed_image_rejected(environment, tmp_path):
    agent, llm, quiz = await advanced_agent(environment, tmp_path)
    await agent.moodle.start(quiz)
    questions = {q.id: (q, i) for q, i in await agent.moodle.questions()}
    question, index = questions["qd"]
    image = await agent.moodle.image(index)
    assert not await agent.browser.page.locator("[data-mentor-badge]").count()
    fresh = next(q for q, i in await agent.moodle.questions() if q.id == "qd")
    assert fresh.fingerprint == question.fingerprint
    output = Path("output/playwright/question-types.png")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(image)
    question, index = questions["qi"]
    answer = await llm.solve(question, await agent.moodle.image(index))
    await agent.browser.page.locator("#qi img").evaluate("n=>n.src=n.src+'#different-image'")
    with pytest.raises(StructureError, match="QUESTION_CHANGED"):
        await agent.moodle.apply(question, index, answer)
    assert not await agent.browser.page.locator("#qi input").nth(1).is_checked()


async def test_broken_image_pauses_without_sending_incomplete_question_to_model(environment, tmp_path):
    agent, llm, quiz = await advanced_agent(environment, tmp_path)
    environment[-1].custom_questions = advanced_questions().replace(TRIANGLE, "/missing.svg")
    agent.s.uncertain_answer_policy = "pause"
    notified = asyncio.Event()

    async def manual(rid, question):
        assert question.id == "qi"
        notified.set()

    agent.notify_manual = manual
    rid = await agent.start(quiz.id)
    try:
        await asyncio.wait_for(notified.wait(), 40)
        assert (await agent.store.run(rid))["state"] == State.PAUSED
        assert len(llm.payloads) == 5
        assert environment[-1].submissions == 0
        assert (await agent.store.run(rid))["stage"] == "MANUAL_INPUT"
    finally:
        await agent.shutdown()


async def test_gapselect_and_embedded_radio_checkbox_groups(environment, tmp_path):
    agent, _, quiz = await setup_agent(environment, tmp_path)
    await agent.moodle.start(quiz)
    await agent.browser.page.set_content("""<div class="que gapselect" id="gap"><div class="content"><div class="qtext">
      Вода: <select name="gap"><option value="0">Choose...</option><option value="opaque-water">H₂O</option></select>
    </div></div></div>
    <div class="que multianswer" id="cloze"><div class="content"><div class="qtext">Выберите чётные и подтвердите:
      <fieldset class="answer"><label><input type="checkbox" name="opaque-one">2</label><label><input type="checkbox" name="opaque-two">3</label><label><input type="checkbox" name="opaque-three">4</label></fieldset>
      <fieldset class="answer"><label><input type="radio" name="confirm" value="a">Да</label><label><input type="radio" name="confirm" value="b">Нет</label></fieldset>
    </div></div></div>""")
    questions = await agent.moodle.questions()
    q, i = questions[0]
    assert q.kind == "matching"
    await agent.moodle.apply(
        q,
        i,
        Answer(
            choices=[],
            text="",
            explanation="test",
            confidence=0.9,
            skip=False,
            assignments=[Assignment(slot="0", choices=["1"], text="")],
        ),
    )
    assert await agent.browser.page.locator("#gap select").input_value() == "opaque-water"
    q, i = (await agent.moodle.questions())[1]
    assert q.kind == "embedded" and [s.kind for s in q.slots] == ["multiple", "single"]
    await agent.moodle.apply(
        q,
        i,
        Answer(
            choices=[],
            text="",
            explanation="test",
            confidence=0.9,
            skip=False,
            assignments=[
                Assignment(slot="0", choices=["0", "2"], text=""),
                Assignment(slot="1", choices=["0"], text=""),
            ],
        ),
    )
    assert await agent.browser.page.locator("#cloze input:checked").count() == 3


@pytest.mark.parametrize("keyboard", [False, True])
async def test_markers_coordinates_copies_and_reapplication(environment, tmp_path, keyboard):
    agent, _, quiz = await setup_agent(environment, tmp_path)
    environment[-1].custom_questions = marker_question()
    await agent.moodle.start(quiz)
    await agent.browser.page.evaluate("enabled=>window.markerKeyboardEnabled=enabled", keyboard)
    question, index = (await agent.moodle.questions())[0]
    assert question.kind == "markers"
    assert [o.capacity for o in question.options] == [2, 1]
    await agent.moodle.image(index)
    answer = Answer(
        choices=[],
        text="",
        explanation="test",
        confidence=0.9,
        skip=False,
        placements=[Placement(choice="0", x=0.2083, y=0.3125), Placement(choice="1", x=0.7639, y=0.3125)],
    )
    await agent.moodle.apply(question, index, answer)
    assert await agent.browser.page.locator(".droparea .marker").count() == 2
    await agent.moodle.apply(question, index, answer)
    assert await agent.browser.page.locator(".droparea .marker").count() == 2
    repeated = answer.model_copy(
        update={
            "placements": [
                Placement(choice="0", x=0.19, y=0.3),
                Placement(choice="0", x=0.22, y=0.3),
                Placement(choice="1", x=0.7639, y=0.3125),
            ]
        }
    )
    # Selection can trigger native text dragging instead of the widget's mouse-up handler.
    await agent.browser.page.locator(".droparea .markertext").first.select_text()
    await asyncio.wait_for(agent.moodle.apply(question, index, repeated), 15)
    assert await agent.browser.page.locator(".droparea .marker.choice1").count() == 2
    invalid = answer.model_copy(
        update={"placements": [Placement(choice="1", x=0.1, y=0.1), Placement(choice="1", x=0.2, y=0.2)]}
    )
    with pytest.raises(ValueError, match="Too many marker"):
        invalid.validate_for(question)
