"""Typed Moodle question adapters. Site control names/values never enter model data."""

import asyncio
import hashlib
import json

from .models import Question
from .privacy import clean


class QuestionError(RuntimeError):
    pass


EXTRACT = r"""async (root, selectors) => {
 const content=root.querySelector('.content') || root.querySelector('.formulation');
 const qtext=root.querySelector(selectors.text);
 const area=root.querySelector(selectors.answers) || qtext;
 if (!content || !qtext) return null;
 const visible=e=>e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
 const all=(selector,node=content)=>[...node.querySelectorAll(selector)];
 const num=(e,key)=>(e.className?.toString().match(new RegExp('(?:^|\\s)'+key+'(\\d+)(?:\\s|$)'))||[])[1];
 const plain=node=>{
   if(!node)return '';
   if(node.tagName==='IMG')return '[изображение: '+(node.alt||'')+']';
   const c=node.cloneNode(true);
   c.querySelectorAll('script[type^="math/tex"],annotation[encoding="application/x-tex"]').forEach(e=>e.replaceWith(' '+e.textContent+' '));
   c.querySelectorAll('[data-mentor-slot]').forEach(e=>e.replaceWith(' [поле '+e.dataset.mentorSlot+'] '));
   c.querySelectorAll('script,style,input,textarea,select,button,[hidden],.accesshide,.sr-only,.visually-hidden').forEach(e=>e.remove());
   c.querySelectorAll('[data-mentor-badge]').forEach(e=>e.remove());
   c.querySelectorAll('img').forEach(e=>e.replaceWith(' [изображение: '+(e.alt||'')+'] '));
   c.querySelectorAll('tr').forEach(e=>e.append('\n'));
   c.querySelectorAll('td,th').forEach(e=>e.append(' | '));
   return c.textContent.trim();
 };
 const mark=(nodes,key)=>{
   const used=nodes.map(e=>e.getAttribute(key)).filter(v=>v!==null);
   let next=used.length?Math.max(...used.map(Number))+1:0;
   nodes.forEach(e=>{if(!e.hasAttribute(key))e.setAttribute(key,String(next++));});
 };
 const result={id:root.id||root.getAttribute('data-question-id'),kind:'unsupported',text:'',options:[],slots:[],
   visual:!!content.querySelector('img,svg,canvas,math,mjx-container')};
 if(root.classList.contains('description'))result.kind='description';
 const ordered=all('.sortablelist > li.sortableitem');
 const drops=all('.drop.active,.dropzone.active');
 const markers=all('.draghomes .marker:not(.dragplaceholder)');
 if(root.classList.contains('ddmarker') && markers.length && content.querySelector('.dropbackground')){
   const numbers=[...new Set(markers.map(e=>num(e,'choice')))].filter(n=>n!==undefined).sort((a,b)=>Number(a)-Number(b));
   numbers.forEach((number,i)=>{
     const nodes=markers.filter(e=>num(e,'choice')===number);
     all('.marker.choice'+number).forEach(e=>e.dataset.mentorChoice=String(i));
     const field=content.querySelector('input.choices.choice'+number);
     const infinite=field?.classList.contains('infinite')||false;
     const capacity=Math.min(100,Number(num(field||nodes[0],'noofdrags')||1));
     result.options.push({id:String(i),text:plain(nodes[0].querySelector('.markertext')||nodes[0]),
       reusable:infinite||capacity>1,capacity:infinite?0:capacity});
   });
   result.kind='markers';result.visual=true;
 }else if(ordered.length>1 && root.classList.contains('ordering')){
   mark(ordered,'data-mentor-item');
   result.kind='ordering';
   result.options=ordered.map(e=>({id:e.dataset.mentorItem,text:plain(e.querySelector('[data-itemcontent]')||e)}))
     .sort((a,b)=>Number(a.id)-Number(b.id));
 }else if(drops.length && (root.classList.contains('ddwtos')||root.classList.contains('ddimageortext'))){
   const groups=new Map();
   all('.draghome').forEach(e=>{
     const group=num(e,'group'),choice=num(e,'choice');
     if(group!==undefined && choice!==undefined){
       const key=group+':'+choice;
       if(!groups.has(key))groups.set(key,[]);
       groups.get(key).push(e);
     }
   });
   const keys=[...groups.keys()].sort((a,b)=>a.localeCompare(b,undefined,{numeric:true}));
   keys.forEach((key,i)=>{
     const nodes=groups.get(key);
     nodes.forEach(e=>e.dataset.mentorChoice=String(i));
     result.options.push({id:String(i),text:plain(nodes[0]),reusable:nodes.some(e=>e.classList.contains('infinite'))});
   });
   drops.sort((a,b)=>Number(num(a,'place'))-Number(num(b,'place'))).forEach((e,i)=>{
     e.dataset.mentorSlot=String(i);
     const group=num(e,'group');
     const options=result.options.filter((o,j)=>keys[j].split(':')[0]===group);
     result.slots.push({id:String(i),kind:'drop',text:e.getAttribute('aria-label')||'Место '+(i+1),options});
   });
   if(result.slots.every(s=>s.options.length))result.kind='dragdrop';
   result.visual ||= root.classList.contains('ddimageortext');
 }else{
   const scope=root.classList.contains('multianswer')||root.classList.contains('gapselect')?qtext:area;
   const controls=scope?all('input:not([type=hidden]):not([type=submit]):not([type=button]),textarea,select',scope)
     .filter(e=>visible(e)&&!e.disabled&&!e.readOnly):[];
   const type=e=>e.tagName==='SELECT'?'select':e.type;
   const isEmbedded=root.classList.contains('multianswer')||root.classList.contains('gapselect');
   if(controls.length && controls.every(e=>type(e)==='radio')&&!isEmbedded&&new Set(controls.map(e=>e.name)).size===1){
     result.kind='single';
   }else if(controls.length && controls.every(e=>type(e)==='checkbox')&&!isEmbedded){
     result.kind='multiple';
   }else if(controls.length===1&&['text','number','textarea'].includes(type(controls[0]))&&!isEmbedded){
     result.kind=root.classList.contains('numerical')||type(controls[0])==='number'?'numeric':'text';
   }else if(controls.length){
     const units=[]; const seen=new Set();
     controls.forEach(e=>{
       if(['radio','checkbox'].includes(type(e))){
         const wrapper=e.closest('.answer,fieldset,.subquestion');
         const key=type(e)==='radio'?e.name:(wrapper||e);
         if(seen.has(key))return;seen.add(key);
         const members=controls.filter(c=>type(c)===type(e)&&(type(e)==='radio'?c.name===e.name:c.closest('.answer,fieldset,.subquestion')===wrapper));
         units.push({nodes:members,kind:type(e)==='radio'?'single':'multiple'});
       }else if(['select','text','number','textarea'].includes(type(e))){
         units.push({nodes:[e],kind:type(e)==='select'?'select':type(e)==='number'?'numeric':'text'});
       }else units.push({nodes:[e],kind:'unsupported'});
     });
     units.forEach((unit,i)=>{
       const e=unit.nodes[0];unit.nodes.forEach(n=>n.dataset.mentorSlot=String(i));
       const row=e.closest('tr');
       const stem=row?.querySelector('.text')||e.labels?.[0];
       let label=plain(stem)||e.getAttribute('aria-label')||'Поле '+(i+1);
       const options=unit.kind==='select'?[...e.options].flatMap((o,j)=>{
         const placeholder=o.value===''||(j===0&&o.value==='0'&&(!o.textContent.trim()||/choose|select|выбер|обер|вибер|^-+$|^…$/i.test(o.textContent)));
         return o.disabled||placeholder?[]:[{id:String(j),text:plain(o)}];
       }):['single','multiple'].includes(unit.kind)?unit.nodes.map((n,j)=>({id:String(j),text:plain(n.labels?.[0]||n.closest('label')||n.parentElement)})):[];
       result.slots.push({id:String(i),text:label,kind:unit.kind,options});
     });
     if(result.slots.every(s=>s.kind!=='unsupported'&&(!['select','single','multiple'].includes(s.kind)||s.options.length)))
       result.kind=controls.every(e=>type(e)==='select')?'matching':'embedded';
   }
   if(['single','multiple'].includes(result.kind)){
     result.options=controls.map((e,i)=>({id:String(i),text:plain(e.labels?.[0]||e.closest('label')||e.parentElement)}));
   }
   if(['single','multiple','text','numeric'].includes(result.kind))controls.forEach((e,i)=>e.dataset.mentorBasic=String(i));
 }
 result.text=plain(qtext);
 const assets=all('img,svg,canvas').map(e=>e.tagName==='IMG'?
   [e.getAttribute('src'),e.getAttribute('srcset')]:e.outerHTML);
 if(assets.length){
   const digest=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify(assets)));
   result.asset_signature=[...new Uint8Array(digest)].map(v=>v.toString(16).padStart(2,'0')).join('');
 }
 return result;
}"""


async def extract(root, selectors):
    raw = await root.evaluate(EXTRACT, selectors)
    if not raw:
        raise QuestionError("QUESTION_TEXT_NOT_FOUND")
    raw["text"] = clean(raw["text"], 24000)
    for option in raw["options"]:
        option["text"] = clean(option["text"], 4000)
    for slot in raw["slots"]:
        slot["text"] = clean(slot["text"], 4000)
        for option in slot["options"]:
            option["text"] = clean(option["text"], 4000)
    raw["fingerprint"] = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
    return Question(**raw)


class QuestionActions:
    def __init__(self, browser):
        self.b = browser

    async def select(self, control, choice):
        await self.b.move(control)
        await control.select_option(index=int(choice))
        await control.press("Tab")
        if not await control.evaluate("(e,i)=>e.selectedIndex===i", int(choice)):
            raise QuestionError("ANSWER_VERIFICATION_FAILED")

    async def slots(self, root, question, answer):
        for assignment in answer.assignments:
            slot = next(s for s in question.slots if s.id == assignment.slot)
            controls = root.locator(f'[data-mentor-slot="{slot.id}"]')
            if slot.kind == "select":
                await self.select(controls, assignment.choices[0])
            elif slot.kind in {"text", "numeric"}:
                await self.b.fill(controls, assignment.text)
            elif slot.kind in {"single", "multiple"}:
                for index, control in enumerate(await controls.all()):
                    if slot.kind == "multiple" or str(index) in assignment.choices:
                        await self.b.check(control, str(index) in assignment.choices)
                actual = [str(i) for i, c in enumerate(await controls.all()) if await c.is_checked()]
                if set(actual) != set(assignment.choices):
                    raise QuestionError("ANSWER_VERIFICATION_FAILED")

    async def ordering(self, root, question, answer):
        items = root.locator(".sortablelist > li.sortableitem")
        for target_index, item_id in enumerate(answer.order):
            item = root.locator(f'[data-mentor-item="{item_id}"]')
            for _ in range(len(answer.order)):
                current = await items.evaluate_all("ns=>ns.map(n=>n.dataset.mentorItem)")
                position = current.index(item_id)
                if position == target_index:
                    break
                backward = item.locator('[data-action="move-backward"]')
                if await backward.count() and await backward.first.is_visible():
                    await self.b.click(backward.first)
                else:
                    await self.b.drag(item, items.nth(position - 1), target_y=0.15)
                await root.page.wait_for_function(
                    "([root,id,position])=>[...root.querySelectorAll('.sortablelist > li.sortableitem')].findIndex(n=>n.dataset.mentorItem===id)!==position",
                    arg=[await root.element_handle(), item_id, position],
                    timeout=2000,
                )
        expected = answer.order
        if await items.evaluate_all("ns=>ns.map(n=>n.dataset.mentorItem)") != expected:
            raise QuestionError("ANSWER_VERIFICATION_FAILED")
        # Check the real response field, not only the animated list order.
        valid = await root.evaluate("""e=>{
          const ids=[...e.querySelectorAll('.sortablelist > li.sortableitem')].map(n=>n.id);
          return [...e.querySelectorAll('input[type=hidden]')].some(n=>n.value===ids.join(','));
        }""")
        if not valid:
            raise QuestionError("ORDER_RESPONSE_NOT_SAVED")

    async def dragdrop(self, root, question, answer):
        # Keyboard cycling is the native Moodle accessible path and works with occupied slots.
        for assignment in answer.assignments:
            target = root.locator(f'[data-mentor-slot="{assignment.slot}"]')
            source_id = assignment.choices[0]
            source = root.locator(f'[data-mentor-choice="{source_id}"]:not(.dragplaceholder)')
            visible = [n for n in await source.all() if await n.is_visible()]
            if not visible:
                raise QuestionError("DRAG_SOURCE_NOT_FOUND")
            source_node = visible[0]
            await self.b.drag(source_node, target)
            if not await self.drop_matches(root, assignment.slot, source_id):
                slot = next(s for s in question.slots if s.id == assignment.slot)
                for _ in range(len(slot.options) + 1):
                    await self.b.move(target)
                    await target.press("ArrowRight")
                    await asyncio.sleep(0.12)
                    if await self.drop_matches(root, assignment.slot, source_id):
                        break
                else:
                    raise QuestionError("ANSWER_VERIFICATION_FAILED")
        # A later drag may displace an earlier answer; recheck all slots together.
        for assignment in answer.assignments:
            if not await self.drop_matches(root, assignment.slot, assignment.choices[0]):
                raise QuestionError("ANSWER_VERIFICATION_FAILED")

    async def drop_matches(self, root, slot_id, choice_id):
        return await root.evaluate(
            r"""(e,[slotId,choiceId])=>{
          const drop=e.querySelector('[data-mentor-slot="'+slotId+'"]');
          const choice=e.querySelector('[data-mentor-choice="'+choiceId+'"]');
          const num=(n,p)=>(n.className.toString().match(new RegExp('(?:^|\\s)'+p+'(\\d+)(?:\\s|$)'))||[])[1];
          if(!drop||!choice)return false;
          const place=num(drop,'place'),group=num(drop,'group'),number=num(choice,'choice');
          const field=e.querySelector('input.placeinput.place'+place+'.group'+group);
          return !!field && field.value===number;
        }""",
            [slot_id, choice_id],
        )


async def screenshot(root, checkpoint):
    await checkpoint()
    content = root.locator(".content")
    if await content.count() != 1:
        content = root.locator(".formulation")
    if await content.count() != 1:
        raise QuestionError("SAFE_IMAGE_REGION_NOT_FOUND")
    ready = await content.evaluate("""async e=>{
       const images=[...e.querySelectorAll('img')].filter(n=>n.getClientRects().length);
       return (await Promise.all(images.map(img=>new Promise(resolve=>{
         if(img.complete)return resolve(img.naturalWidth>0);
         const timeout=setTimeout(()=>resolve(false),10000);
         const done=()=>{clearTimeout(timeout);resolve(img.naturalWidth>0)};
         img.addEventListener('load',done,{once:true});img.addEventListener('error',done,{once:true});
       })))).every(Boolean);
    }""")
    if not ready:
        raise QuestionError("QUESTION_IMAGE_NOT_READY")
    await checkpoint()
    if await root.evaluate("e=>e.classList.contains('ddmarker')"):
        # The model returns normalized background coordinates. Give it exactly this frame,
        # without blank page margins that could be mistaken for part of the coordinate system.
        return await content.locator(".dropbackground").screenshot(type="png", animations="disabled")
    # Local labels tie the screenshot to typed option/slot IDs, without exporting site attributes.
    original_position = await content.evaluate("""e=>{
      const original=e.style.position;
      if(getComputedStyle(e).position==='static')e.style.position='relative';
      const parent=e.getBoundingClientRect();
      e.querySelectorAll('[data-mentor-slot],[data-mentor-choice],[data-mentor-item],[data-mentor-basic]').forEach(n=>{
        if(!n.getClientRects().length||n.matches('.dragplaceholder'))return;
        const node=n.labels?.[0]||n;
        const rect=node.getBoundingClientRect();
        const span=document.createElement('span');span.dataset.mentorBadge='1';
        span.textContent=n.hasAttribute('data-mentor-slot')?'[поле '+n.dataset.mentorSlot+']':
          '[вариант '+(n.dataset.mentorChoice??n.dataset.mentorItem??n.dataset.mentorBasic)+']';
        span.style.cssText='position:absolute;z-index:100;background:#fff090;color:#000;font:12px sans-serif;padding:1px;pointer-events:none;';
        span.style.left=Math.max(0,rect.left-parent.left)+'px';
        span.style.top=Math.max(0,rect.top-parent.top-12)+'px';
        e.append(span);
      });
      return original;
    }""")
    try:
        return await content.screenshot(type="png", animations="disabled")
    finally:
        await content.evaluate(
            "(e,position)=>{e.querySelectorAll('[data-mentor-badge]').forEach(n=>n.remove());e.style.position=position}",
            original_position,
        )
