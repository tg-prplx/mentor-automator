import base64
import struct
from urllib.parse import quote

from mentor_automator.questions import extract, screenshot


async def test_offscreen_image_answers_included_in_payload_and_full_question_capture(environment):
    browser = environment[2]
    page = browser.page
    await page.goto(environment[0].moodle_url + "/login/index.php")
    await page.set_viewport_size({"width": 640, "height": 360})
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="60"><rect width="120" height="60" fill="rgb(17,231,93)"/></svg>'
    image_url = "data:image/svg+xml," + quote(svg)
    options = "".join(
        f'<label style="display:block;height:120px" for="answer-{i}">'
        f'<input id="answer-{i}" type="radio" name="answer">Ответ {i}'
        f'<img alt="Схема {i}" src="{image_url}"></label>'
        for i in range(14)
    )
    await page.set_content(
        "<style>body{margin:0}.content{width:600px;padding:12px;box-sizing:border-box}</style>"
        '<div class="que" id="long-question"><div class="content">'
        '<div class="qtext">Выберите схему</div>'
        f'<div class="answer">{options}</div></div></div>'
    )
    root = page.locator("#long-question")
    content = root.locator(".content")
    question = await extract(root, {"text": ".qtext", "answers": ".answer"})
    assert question.kind == "single" and question.visual
    assert len(question.payload()["options"]) == 14
    assert question.options[-1].id == "13" and "Схема 13" in question.options[-1].text
    last_image = root.locator("img").last
    await last_image.evaluate("img=>img.decode()")
    last_box = await last_image.bounding_box()
    content_box = await content.bounding_box()
    assert last_box["y"] > 4 * page.viewport_size["height"]
    # Test the exact capture function used for model input, including its local option labels.
    png = await screenshot(root, browser.checkpoint)
    width, height = struct.unpack(">II", png[16:24])
    assert width == int(content_box["width"])
    assert height >= int(content_box["height"]) > 4 * page.viewport_size["height"]
    x = int(last_box["x"] - content_box["x"] + last_box["width"] / 2)
    y = int(last_box["y"] - content_box["y"] + last_box["height"] / 2)
    pixel = await page.evaluate(
        """async ({source,x,y})=>{
          const image=new Image();image.src=source;await image.decode();
          const canvas=document.createElement('canvas');canvas.width=image.width;canvas.height=image.height;
          const context=canvas.getContext('2d');context.drawImage(image,0,0);
          return [...context.getImageData(x,y,1,1).data];
        }""",
        {"source": "data:image/png;base64," + base64.b64encode(png).decode(), "x": x, "y": y},
    )
    assert pixel == [17, 231, 93, 255]  # the last offscreen image is actually present in the PNG
    assert not await root.locator("[data-mentor-badge]").count()
