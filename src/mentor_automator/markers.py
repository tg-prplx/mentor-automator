"""Visible placement of Moodle ddmarker controls; hidden fields are read only."""

import asyncio

from .questions import QuestionError

VERIFY = r"""(root,placements)=>{
 const image=root.querySelector('.dropbackground');
 const counts=new Map();
 for(const p of placements){
   const marker=root.querySelector('[data-mentor-choice="'+p.choice+'"]');
   const choice=marker?.className.match(/(?:^|\s)choice(\d+)(?:\s|$)/)?.[1];
   const input=root.querySelector('input.choices.choice'+choice);
   const positions=(input?.value||'').split(';').filter(Boolean).map(v=>v.split(',').map(Number));
   const used=counts.get(p.choice)||new Set();
   const match=positions.findIndex(([x,y],i)=>!used.has(i)&&
     Math.abs(x/image.naturalWidth-p.x)<=Math.max(.015,3/image.naturalWidth)&&
     Math.abs(y/image.naturalHeight-p.y)<=Math.max(.015,3/image.naturalHeight));
   if(match<0)return false;used.add(match);counts.set(p.choice,used);
 }
 return [...counts].every(([id,used])=>{
   const marker=root.querySelector('[data-mentor-choice="'+id+'"]');
   const choice=marker.className.match(/(?:^|\s)choice(\d+)(?:\s|$)/)[1];
   return root.querySelector('input.choices.choice'+choice).value.split(';').filter(Boolean).length===used.size;
 });
}"""


async def apply_markers(browser, root, answer):
    background = root.locator(".dropbackground")
    homes = root.locator(".draghomes")
    for _ in range(100):
        placed = root.locator(".droparea .marker:not(.dragplaceholder)")
        before = await placed.count()
        if not before:
            break
        # Moodle ddmarker supports Escape to return a focused marker home.
        # It avoids native text dragging and preserves the widget's own save/change events.
        await browser.checkpoint()
        await placed.first.press("Escape")
        await asyncio.sleep(0.15)
        await browser.checkpoint()
        if await placed.count() >= before:
            # Older/custom widgets may only support mouse interaction.
            box = await placed.first.bounding_box()
            if not box:
                raise QuestionError("MARKER_RESET_FAILED")
            await browser.drag(
                placed.first,
                homes,
                source_x=min(1.0 / box["width"], 0.5),
                source_y=min(1.0 / box["height"], 0.5),
            )
            await asyncio.sleep(0.15)
        if await placed.count() >= before:
            raise QuestionError("MARKER_RESET_FAILED")
    if not await root.locator("input.choices").evaluate_all("ns=>ns.every(n=>!n.value)"):
        raise QuestionError("MARKER_RESET_FAILED")
    completed = []
    for placement in answer.placements:
        source = homes.locator(f'[data-mentor-choice="{placement.choice}"]:not(.dragplaceholder)')
        nodes = [node for node in await source.all() if await node.is_visible()]
        if not nodes:
            raise QuestionError("DRAG_SOURCE_NOT_FOUND")
        box = await nodes[0].bounding_box()
        if not box:
            raise QuestionError("DRAG_SOURCE_NOT_FOUND")
        await browser.drag(
            nodes[0],
            background,
            target_x=placement.x,
            target_y=placement.y,
            source_x=min(1.0 / box["width"], 0.5),
            source_y=min(1.0 / box["height"], 0.5),
        )
        await asyncio.sleep(0.15)
        completed.append(placement.model_dump())
        if not await root.evaluate(VERIFY, completed):
            raise QuestionError("MARKER_POSITION_NOT_SAVED")
