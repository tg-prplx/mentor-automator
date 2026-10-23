"""Synthetic UI contracts based on Moodle 4.5 renderers; no account HTML or data."""

from urllib.parse import quote


def image(svg):
    return "data:image/svg+xml," + quote(svg)


TRIANGLE = image(
    '<svg xmlns="http://www.w3.org/2000/svg" width="220" height="120"><polygon points="110,10 15,110 205,110" fill="#1778cc"/></svg>'
)
BACKGROUND = image(
    '<svg xmlns="http://www.w3.org/2000/svg" width="360" height="160"><rect width="360" height="160" fill="white"/><rect x="35" y="10" width="80" height="80" fill="red"/><circle cx="275" cy="50" r="40" fill="blue"/></svg>'
)


def marker_question():
    return rf'''<div class="que ddmarker" id="qmarkers"><div class="content">
    <div class="qtext">Поставьте маркер «квадрат» в центр красного квадрата и маркер «круг» в центр синего круга.</div>
    <div class="ddarea"><div class="droparea" style="position:relative;width:360px;height:160px">
      <img class="dropbackground" alt="" src="{BACKGROUND}">
    </div><div class="draghomes" style="margin-top:20px;min-height:40px">
      <span class="marker choice1 dragno2" style="display:inline-block;padding:6px;border:1px solid black"><span class="markertext">квадрат</span></span>
      <span class="marker choice2 dragno1" style="display:inline-block;padding:6px;border:1px solid black"><span class="markertext">круг</span></span>
    </div></div><div class="ddform">
    <input type="hidden" class="choices choice1 noofdrags2" name="qmarkers_1" value="">
    <input type="hidden" class="choices choice2 noofdrags1" name="qmarkers_2" value="">
    </div></div></div>
    <script>
    let marker=null;
    const root=document.querySelector('#qmarkers');
    const number=n=>n.className.match(/(?:^|\s)choice(\d+)(?:\s|$)/)[1];
    const save=()=>root.querySelectorAll('input.choices').forEach(input=>{{
      input.value=[...root.querySelectorAll('.droparea .marker.choice'+number(input))].map(n=>n.dataset.coords).join(';');
    }});
    document.addEventListener('mousedown',e=>{{marker=e.target.closest('.marker')}});
    document.addEventListener('mouseup',e=>{{
      if(!marker)return;
      const bg=root.querySelector('.dropbackground'),rect=bg.getBoundingClientRect();
      if(e.clientX>=rect.left&&e.clientX<rect.right&&e.clientY>=rect.top&&e.clientY<rect.bottom){{
        const node=marker.closest('.droparea')?marker:marker.cloneNode(true);
        const x=Math.round((e.clientX-rect.left-1)/rect.width*bg.naturalWidth);
        const y=Math.round((e.clientY-rect.top-1)/rect.height*bg.naturalHeight);
        node.dataset.coords=x+','+y;
        node.style.cssText='position:absolute;left:'+x+'px;top:'+y+'px;background:#fff090;';
        root.querySelector('.droparea').append(node);
      }}else if(marker.closest('.droparea'))marker.remove();
      save();marker=null;
    }});
    </script>'''


def advanced_questions():
    return rf'''
    <style>
      .que {{margin:20px;padding:12px;border:1px solid #777;font:18px sans-serif}}
      .sortablelist li {{min-height:44px;padding:8px;border:1px solid #aaa;list-style:none}}
      .drop,.dropzone,.draghome {{display:inline-block;min-width:85px;min-height:28px;border:2px solid #777;padding:5px}}
      .droparea {{position:relative;width:360px;height:160px}} .dropzone {{position:absolute;top:105px}}
      .dropzone.place1 {{left:20px}} .dropzone.place2 {{left:220px}}
      .draghomes {{margin-top:12px}} .draghome {{cursor:grab;background:#eee}}
    </style>
    <div class="que match" id="qm"><div class="content"><div class="qtext">Сопоставьте страны и столицы.</div>
      <table class="answer"><tr><td class="text">Франция</td><td><select name="qm_0"><option value="0">Choose...</option><option value="opaque-berlin">Берлин</option><option value="opaque-paris">Париж</option></select></td></tr>
      <tr><td class="text">Германия</td><td><select name="qm_1"><option value="0">Choose...</option><option value="opaque-berlin">Берлин</option><option value="opaque-paris">Париж</option></select></td></tr></table>
    </div></div>
    <div class="que multianswer" id="qe"><div class="content"><div class="qtext">
      Заполните: 2+2=<span class="subquestion"><input type="text" name="qe_0"></span>;
      Спутник Земли: <select name="qe_1"><option value="">Выберите</option><option value="opaque-sun">Солнце</option><option value="opaque-moon">Луна</option></select>.
    </div></div></div>
    <div class="que ordering" id="qo"><div class="content"><div class="qtext">Расположите: Первый, Второй, Третий.</div>
      <ul class="sortablelist active">
        <li class="sortableitem" id="opaque-b"><span data-itemcontent>Второй</span><button type="button" data-action="move-backward">Вверх</button></li>
        <li class="sortableitem" id="opaque-c"><span data-itemcontent>Третий</span><button type="button" data-action="move-backward">Вверх</button></li>
        <li class="sortableitem" id="opaque-a"><span data-itemcontent>Первый</span><button type="button" data-action="move-backward">Вверх</button></li>
      </ul><input type="hidden" name="qo_response" value="opaque-b,opaque-c,opaque-a">
    </div></div>
    <div class="que ddwtos" id="qw"><div class="content"><div class="qtext">
      Вода замерзает при <span class="drop active place1 group1" tabindex="0"></span> °C,
      кипит при <span class="drop active place2 group1" tabindex="0"></span> °C.
    </div><div class="answercontainer"><span class="draghome choice1 group1">100</span><span class="draghome choice2 group1">0</span></div>
      <input class="placeinput place1 group1" type="hidden" name="qw_1" value="0">
      <input class="placeinput place2 group1" type="hidden" name="qw_2" value="0">
    </div></div>
    <div class="que ddimageortext" id="qd"><div class="content"><div class="qtext">Поставьте подпись под соответствующей фигурой.</div>
      <div class="ddarea"><div class="droparea"><img class="dropbackground" alt="" src="{BACKGROUND}">
        <div class="dropzones"><div class="dropzone active place1 group1" tabindex="0"></div><div class="dropzone active place2 group1" tabindex="0"></div></div>
      </div><div class="draghomes"><span class="draghome choice1 group1">Синий круг</span><span class="draghome choice2 group1">Красный квадрат</span></div></div>
      <input class="placeinput place1 group1" type="hidden" name="qd_1" value="0">
      <input class="placeinput place2 group1" type="hidden" name="qd_2" value="0">
    </div></div>
    <div class="que multichoice" id="qi"><div class="content"><div class="qtext">Какая фигура изображена? <img alt="" src="{TRIANGLE}"></div>
      <div class="answer"><label><input type="radio" name="qi" value="opaque-circle">Круг</label><label><input type="radio" name="qi" value="opaque-triangle">Треугольник</label></div>
    </div></div>
    <script>
    document.querySelector('#qo').addEventListener('click',e=>{{
      if(!e.target.matches('[data-action="move-backward"]'))return;
      const item=e.target.closest('li');if(item.previousElementSibling)item.parentElement.insertBefore(item,item.previousElementSibling);
      document.querySelector('[name=qo_response]').value=[...item.parentElement.children].map(n=>n.id).join(',');
    }});
    const suffix=(n,p)=>n.className.match(new RegExp('(?:^|\\s)'+p+'(\\d+)(?:\\s|$)'))?.[1];
    const put=(drop,source)=>{{
      const root=drop.closest('.que'),number=suffix(source,'choice'),place=suffix(drop,'place');
      root.querySelectorAll('input.placeinput').forEach(input=>{{if(input.value===number)input.value='0'}});
      root.querySelector('input.placeinput.place'+place).value=number;
      drop.textContent=source.textContent;
    }};
    let dragged=null;
    document.addEventListener('mousedown',e=>{{dragged=e.target.closest('.draghome')}});
    document.addEventListener('mouseup',e=>{{const drop=e.target.closest('.drop,.dropzone');if(dragged&&drop&&dragged.closest('.que')===drop.closest('.que'))put(drop,dragged);dragged=null}});
    let ordered=null;
    document.addEventListener('mousedown',e=>{{ordered=e.target.closest('.sortableitem')}});
    document.addEventListener('mouseup',e=>{{
      const target=e.target.closest('.sortableitem');
      if(ordered&&target&&ordered!==target){{
        target.parentElement.insertBefore(ordered,target);
        document.querySelector('[name=qo_response]').value=[...target.parentElement.children].map(n=>n.id).join(',');
      }}
      ordered=null;
    }});
    document.addEventListener('keydown',e=>{{
      if(e.key!=='ArrowRight'||!e.target.matches('.drop,.dropzone'))return;
      e.preventDefault();const drop=e.target,root=drop.closest('.que');
      const options=[...root.querySelectorAll('.draghome')];
      const field=root.querySelector('input.placeinput.place'+suffix(drop,'place'));
      const index=options.findIndex(n=>suffix(n,'choice')===field.value);
      put(drop,options[(index+1)%options.length]);
    }});
    </script>'''
