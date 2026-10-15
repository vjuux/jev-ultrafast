(() => {
  if (!document.body) return null;
  const cache = window.__jevFast ||= {ids:new WeakMap(), nodes:new Map(), next:1};
  const identity = e => {
    if (!cache.ids.has(e)) cache.ids.set(e,cache.next++);
    const id=cache.ids.get(e); cache.nodes.set(id,e); return id;
  };
  for (const [id,e] of cache.nodes) if (!e.isConnected) cache.nodes.delete(id);
  const safe = e => !['password','file','hidden'].includes(e.type);
  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const name = (e,seen=new Set()) => {
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const referenced=(e.getAttribute('aria-labelledby')||'').split(/\s+/)
      .map(id=>name(document.getElementById(id),seen)).filter(Boolean).join(' ');
    return referenced || e.getAttribute('aria-label') ||
      [...(e.labels||[])].map(l=>name(l,seen)).filter(Boolean).join(' ') ||
      (['button','submit','reset'].includes(e.type) ? e.value : '') || e.getAttribute('alt') ||
      (e.tagName==='INPUT' ? '' : [...e.childNodes].map(n=>n.nodeType===3 ? n.textContent :
        n.nodeType===1 && n.getAttribute('aria-hidden')!=='true' ? name(n,seen) : '').join(' ').trim()) ||
      e.getAttribute('title') || e.getAttribute('placeholder') || '';
  };
  const roles=['button','link','checkbox','radio','switch','tab','menuitem','menuitemradio',
    'option','gridcell','combobox','textbox','searchbox','spinbutton'];
  const selector='a[href],button,input,textarea,select,summary,[contenteditable="true"],'+
    roles.map(role=>'[role="'+role+'"]').join(',');
  const role = e => {
    const explicit=e.getAttribute('role');
    if (roles.includes(explicit)) return explicit;
    if (e.tagName==='BUTTON' || e.tagName==='SUMMARY') return 'button';
    if (e.tagName==='A') return 'link';
    if (e.tagName==='SELECT') return 'combobox';
    if (e.tagName==='TEXTAREA' || e.isContentEditable) return 'textbox';
    if (e.tagName==='INPUT') {
      if (['checkbox','radio'].includes(e.type)) return e.type;
      if (['button','submit','reset','image'].includes(e.type)) return 'button';
      if (e.type==='search') return 'searchbox';
      if (e.type==='number') return 'spinbutton';
      if (['text','email','url','tel'].includes(e.type)) return 'textbox';
    }
    return null;
  };
  cache.pageKey=()=>[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    [...document.querySelectorAll('input,textarea,select')].filter(safe)
      .map(e=>[identity(e),e.value,e.checked,e.selectedIndex,e.disabled,e.readOnly])];
  cache.guard=e=>{
    if (!e?.isConnected || !visible(e)) return null;
    const scope=e.closest('form,dialog,[role="dialog"],article,li,tr,[role="row"]') || e.parentElement;
    return [identity(e),role(e),name(e),e.value??null,e.checked??null,e.selectedIndex??null,
      e.readOnly??null,e.matches(':disabled'),e.getAttribute('aria-disabled'),
      e.getAttribute('aria-expanded'),e.getAttribute('aria-checked'),e.getAttribute('aria-selected'),
      e.getAttribute('href'),scope?.innerText?.slice(0,6000)||''];
  };
  // Widgets whose raw controls need counting (a calendar's month arrows, a stepper's +/-) are offered as ONE fill
  // action ("set the date" / "set the number") instead of the arrows; the executor clicks until the value is reached.
  const MONTH_TITLE=/\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})\b/;
  const bname=b=>(b.getAttribute('aria-label')||b.getAttribute('title')||b.textContent||'').trim();
  const NEXT=/^(next( month)?|›|»|>|→)$/i, PREV=/^(prev(ious)?( month)?|‹|«|<|←)$/i;
  const INC=/^(increase|increment|add|plus|more)\b|^\+$/i, DEC=/^(decrease|decrement|remove|minus|fewer|less)\b|^[−–-]$/i;
  const clickable='button,[role="button"],a';
  const macros=[], hidden=new Set();
  for (const b of __JEV_MACROS__ ? document.querySelectorAll(clickable) : []) {
    if (!visible(b) || !NEXT.test(bname(b))) continue;
    let box=b.parentElement;
    while (box && box!==document.body && !(MONTH_TITLE.test(box.innerText||'') &&
        box.querySelectorAll('button,[role="gridcell"],td a').length>=28)) box=box.parentElement;
    if (!box || box===document.body || macros.some(m=>m.box===box)) continue;
    hidden.add(b);
    for (const p of box.querySelectorAll(clickable)) if (PREV.test(bname(p))) hidden.add(p);
    const owner=box.closest('[role="dialog"]')?.getAttribute('aria-label') || box.getAttribute('aria-label') || 'Calendar';
    macros.push({box,macro:'date',label:owner+': set the date',value:(box.innerText.match(MONTH_TITLE)||[''])[0]});
  }
  for (const inc of __JEV_MACROS__ ? document.querySelectorAll(clickable) : []) {
    if (!visible(inc) || !INC.test(bname(inc))) continue;
    const box=inc.parentElement, dec=box && [...box.querySelectorAll(clickable)].find(x=>DEC.test(bname(x)));
    const num=dec && (box.innerText||'').replace(/[+−–-]/g,' ').match(/\b\d{1,3}\b/);
    if (!num || macros.some(m=>m.box===box)) continue;
    hidden.add(inc); hidden.add(dec);
    const noun=(box.innerText||'').replace(/[+−–-]|\b\d{1,3}\b/g,' ').replace(/\s+/g,' ').trim() ||
      bname(inc).replace(INC,'').trim() || 'Count';
    macros.push({box,macro:'count',label:noun+': set the number',value:num[0]});
  }
  const actions=[];
  for (const e of document.querySelectorAll(selector)) {
    if (hidden.has(e)) continue;
    if (!safe(e) || !visible(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"]')) continue;
    const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2, rname=role(e);
    // an option scrolled out of view INSIDE an open list (a long time / country list) is offered too: the executor
    // scrolls it into view before clicking, so the policy never has to scroll a list it cannot see
    const inList=['option','menuitem','menuitemradio'].includes(rname) && (()=>{
      for (let p=e.parentElement; p && p!==document.body; p=p.parentElement) {
        const o=getComputedStyle(p).overflowY;
        if ((o==='auto'||o==='scroll') && p.scrollHeight>p.clientHeight+2) {
          const q=p.getBoundingClientRect(); return q.bottom>0 && q.top<innerHeight && visible(p);
        }
      }
      return false;
    })();
    const inView=x>=0 && y>=0 && x<innerWidth && y<innerHeight;
    if (!rname || r.width<=0 || r.height<=0 || (!inView && !inList)) continue;
    if (rname==='gridcell' && e.querySelector('button,[role="button"]')) continue;
    // Covered by an unrelated element (a modal, an open search overlay, a sticky banner): it cannot be clicked, so it
    // is not offered.  Covered by its own ancestor or descendant is fine (browser.py clicks through an ancestor).
    const hit=inView ? document.elementFromPoint(x,y) : null;
    if (hit && hit!==e && !e.contains(hit) && !hit.contains(e) && !(e.labels && [...e.labels].some(l=>l.contains(hit)))) continue;
    const base={node:identity(e),role:rname,label:name(e)||rname,
      rect:{x:r.x,y:r.y,w:r.width,h:r.height}};
    for (const key of ['checked','selected','expanded']) {
      const value=e.getAttribute('aria-'+key);
      if (value!==null) base[key]=value;
    }
    if (['checkbox','radio'].includes(e.type)) base.checked=String(e.checked);
    if (e.tagName==='SELECT') {
      for (const o of e.options) if (!o.selected && !o.disabled && !o.closest('optgroup[disabled]'))
        actions.push({...base,kind:'select',value:o.value,
          current_value:[...e.selectedOptions].map(o=>o.label).join(', '),label:base.label+' → '+o.label});
    } else {
      const editable=!e.readOnly && e.getAttribute('aria-readonly')!=='true' &&
        (['textbox','searchbox','spinbutton'].includes(rname) ||
          (rname==='combobox' && ['INPUT','TEXTAREA'].includes(e.tagName)));
      const value='value' in e ? String(e.value) :
        e.isContentEditable || rname==='combobox' ? e.innerText.trim() : '';
      // format hints for the text helper (a date field that only accepts MM/DD/YYYY, a phone pattern, ...)
      const hints=editable ? Object.fromEntries([['placeholder',e.getAttribute('placeholder')],['input_type',e.type],
        ['pattern',e.getAttribute('pattern')],['maxlength',e.getAttribute('maxlength')],
        ['format_hint',(e.getAttribute('aria-describedby')||'').split(/\s+/).map(id=>document.getElementById(id)?.innerText||'').join(' ').trim()]]
        .filter(([k,v])=>v && !(k==='input_type' && v==='text'))) : {};
      actions.push({...base,kind:editable?'fill':'click',value,...hints});
      if (editable) actions.push({...base,kind:'click',value,label:'Open '+base.label});
    }
  }
  for (const m of macros) {
    const r=m.box.getBoundingClientRect();
    if (!visible(m.box) || r.width<=0 || r.height<=0 || r.bottom<0 || r.top>=innerHeight) continue;
    actions.push({node:identity(m.box),role:'textbox',label:m.label,kind:'fill',macro:m.macro,value:m.value,
      rect:{x:r.x,y:r.y,w:r.width,h:r.height}});
  }
  const words=[], walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
  const range=document.createRange(); let node,length=0;
  while ((node=walker.nextNode()) && length<6000) {
    const value=node.textContent.trim(), parent=node.parentElement;
    if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent)) continue;
    range.selectNodeContents(node); const r=range.getBoundingClientRect();
    if (r.width>0 && r.height>0 && r.bottom>0 && r.top<innerHeight && r.right>0 && r.left<innerWidth) {
      words.push(value); length+=value.length;
    }
  }
  const text=words.join('\n').slice(0,6000), height=document.documentElement.scrollHeight;
  const page_key=cache.pageKey(), guards={};
  for (const a of actions) if (!(a.node in guards)) guards[a.node]=cache.guard(cache.nodes.get(a.node));
  // Compare meaning and identity. Geometry is always resolved and hit-tested just before input.
  const semantics=actions.map(({rect,...action})=>action);
  const marker=[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    document.title,text,semantics,page_key[6]];
  const omitted_actions=Math.max(0,actions.length-250);
  actions.splice(250);
  actions.forEach((a,i)=>a.id='e'+(i+1));
  if (scrollY+innerHeight<height-2) actions.push({id:'scroll_down',kind:'scroll',label:'Scroll down',delta:560});
  if (scrollY>0) actions.push({id:'scroll_up',kind:'scroll',label:'Scroll up',delta:-560});
  const ae=document.activeElement;
  if (ae && (ae.tagName==='TEXTAREA' || (ae.tagName==='INPUT' && ['text','search','url','email','tel','number'].includes(ae.type)))
      && safe(ae) && visible(ae) && (ae.value||'').trim())
    actions.push({id:'press_enter',kind:'key',label:'Press Enter in the focused text field (submit it)',key:'Enter'});
  actions.push({id:'wait',kind:'wait',label:'Wait for the page to update'});
  return {url:location.href,title:document.title,w:innerWidth,h:innerHeight,text,
    scroll:{y:scrollY,height},actions,marker,page_key,guards,omitted_actions};
})()
