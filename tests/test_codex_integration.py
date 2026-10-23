"""Opt-in real Codex against synthetic Moodle only; never loads the project .env."""

import asyncio
import os

import pytest
from question_fixtures import marker_question
from test_browser_agent import setup_agent
from test_question_types import advanced_agent

from mentor_automator.llm import LLM
from mentor_automator.models import State


@pytest.mark.skipif(os.environ.get("MENTOR_LIVE_CODEX") != "1", reason="Explicit real Codex opt-in")
async def test_real_codex_delayed_multipart_moodle(environment, tmp_path):
    state = environment[-1]
    state.start_mode = "mentor"
    state.render_delay = 800
    state.form_enctype = "multipart/form-data"
    agent, _, quiz = await setup_agent(environment, tmp_path)
    agent.s.llm_timeout = 90
    agent.s.llm_retries = 0
    agent.llm = LLM(agent.s)
    rid = await agent.start(quiz.id)
    await asyncio.wait_for(agent.task, 300)
    row = await agent.store.run(rid)
    assert row["state"] == State.AWAITING_FINAL_CONFIRMATION, row["error_code"]
    assert await agent.store.counts(rid) == (4, 1)
    assert len(state.saves) == 2
    assert state.saves[0]["q1"] == "1"
    assert state.saves[1]["q4"].replace(",", ".") == "2.5"
    assert state.starts == 1 and state.submissions == 0
    assert not agent.browser.gate.allowed("10")


@pytest.mark.skipif(os.environ.get("MENTOR_LIVE_CODEX") != "1", reason="Explicit real Codex opt-in")
async def test_real_codex_matching_ordering_dragdrop_and_image(environment, tmp_path):
    agent, _, quiz = await advanced_agent(environment, tmp_path)
    agent.s.llm_timeout = 90
    agent.s.llm_retries = 0
    agent.llm = LLM(agent.s)
    rid = await agent.start(quiz.id)
    await asyncio.wait_for(agent.task, 360)
    row = await agent.store.run(rid)
    assert row["state"] == State.AWAITING_FINAL_CONFIRMATION, row["error_code"]
    assert await agent.store.counts(rid) == (8, 1)
    assert environment[-1].submissions == 0
    saved = environment[-1].saves[0]
    assert saved["qm_0"] == "opaque-paris" and saved["qm_1"] == "opaque-berlin"
    assert saved["qo_response"] == "opaque-a,opaque-b,opaque-c"
    assert (saved["qw_1"], saved["qw_2"]) == ("2", "1")
    assert (saved["qd_1"], saved["qd_2"]) == ("2", "1")
    assert saved["qi"] == "opaque-triangle"


@pytest.mark.skipif(os.environ.get("MENTOR_LIVE_CODEX") != "1", reason="Explicit real Codex opt-in")
async def test_real_codex_marker_positions(environment, tmp_path):
    agent, _, quiz = await setup_agent(environment, tmp_path)
    environment[-1].custom_questions = marker_question()
    await agent.moodle.start(quiz)
    question, index = (await agent.moodle.questions())[0]
    image = await agent.moodle.image(index)
    agent.s.llm_retries = 0
    answer = await LLM(agent.s).solve(question, image)
    assert not answer.skip and len(answer.placements) == 2
    await agent.moodle.apply(question, index, answer)
    red = await agent.browser.page.locator("[name=qmarkers_1]").input_value()
    blue = await agent.browser.page.locator("[name=qmarkers_2]").input_value()
    x, y = map(float, red.split(","))
    assert 35 < x < 115 and 10 < y < 90
    x, y = map(float, blue.split(","))
    assert (x - 275) ** 2 + (y - 50) ** 2 < 40**2
    assert environment[-1].submissions == 0
