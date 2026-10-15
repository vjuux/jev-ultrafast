"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp

# Atomically read visible content and controls, preserving actual DOM node identity.
# JEV_MACROS=1: calendars and steppers are offered as one "set the date / number" action (see snapshot.js)
READ_STATE = Path(__file__).with_name("snapshot.js").read_text().replace(
    "__JEV_MACROS__", "true" if os.environ.get("JEV_MACROS") == "1" else "false")
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"

class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class Browser:
    def __init__(self, url):
        ensure_daemon()
        self.target = cdp("Target.createTarget", url="about:blank", background=True)["targetId"]
        self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
        self.call("Emulation.setDeviceMetricsOverride", width=1120, height=780, deviceScaleFactor=1, mobile=False)
        # Keep rAF/menus rendering in an owned background tab, without activating the user's Chrome tab.
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        self.call("Page.navigate", url=url)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.evaluate("document.readyState") == "complete":
                break
            time.sleep(0.02)

    def call(self, method, **params):
        return cdp(method, session_id=self.session, **params)

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def observe(self, screenshot=True):
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            # This is read-only and happens after execution was logged, even if navigation interrupts it.
            try:
                self.call(
                    "Runtime.evaluate",
                    expression="""(action => new Promise(resolve => {
                      const field=window.__jevFast?.nodes.get(action.node);
                      const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
                      let frames=0, stopped=false;
                      const finish=()=>{stopped=true;resolve()};
                      setTimeout(finish,autocomplete ? 200 : 50);
                      const ready=()=>{
                        if (stopped) return;
                        const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
                          .split(/\\s+/).filter(Boolean);
                        const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
                        const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
                        if (++frames>=2 && (!autocomplete || options.some(e=>{
                          const r=e.getBoundingClientRect();
                          return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
                            e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                        }))) finish();
                        else requestAnimationFrame(ready);
                      };
                      requestAnimationFrame(ready);
                    }))(""" + json.dumps(action) + ")",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except RuntimeError:
                pass
        for attempt in range(10):
            try:
                return browser_operation(
                    {"operation": "observe", "session": self.session, "screenshot": screenshot}
                )
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.02)
        raise StalePage("Page did not settle")

    def fresh(self, page, action=None):
        if action is not None and action["kind"] in {"click", "select"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    def act(self, action, page, text=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            time.sleep(0.1)
        before = self._doc_key()
        result = browser_operation({"operation": "act", "session": self.session, "action": action, "text": text})
        self.after_input = action if action["kind"] in {"click", "fill", "select"} else None
        if action["kind"] in {"key", "click", "select"}:
            self._settle(before)
        return result

    def _doc_key(self):
        try:
            return self.evaluate("[performance.timeOrigin, location.href, document.readyState]")
        except Exception:
            return None

    def _settle(self, before, quiet=0.6, limit=6.0):
        """Wait for a navigation the action started (Enter in a search box, a submit button, a link) to land, so the
        next observation is the NEW page -- not the old one, which made the agent think Enter 'changed nothing' and
        then stop on the stale page.  Returns quickly when nothing navigates."""
        t0 = time.monotonic(); moved = False
        while time.monotonic() - t0 < limit:
            now = self._doc_key()
            if now is None:                       # document is being replaced
                moved = True
            elif before is None or now[0] != before[0] or now[1] != before[1]:
                moved = True
                if now[2] == "complete":
                    time.sleep(0.3); return
            elif not moved and time.monotonic() - t0 > quiet:
                return                            # nothing navigated
            time.sleep(0.1)

    def close(self):
        if self.target:
            cdp("Target.closeTarget", targetId=self.target)
            self.target = None


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November",
          "December"]

# In-page helpers for the widget macros: read the calendar's shown month / the stepper's number, click one control.
MACRO_JS = r"""((node, op, arg) => {
  const box=window.__jevFast?.nodes.get(node);
  if (!box?.isConnected) return null;
  const vis=e=>e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}) && !e.matches(':disabled') &&
    e.getAttribute('aria-disabled')!=='true';
  const nm=b=>(b.getAttribute('aria-label')||b.getAttribute('title')||b.textContent||'').trim();
  const btns=[...box.querySelectorAll('button,[role="button"],[role="gridcell"],a,td')].filter(vis);
  const M=/\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})\b/;
  const re={next:/^(next( month)?|›|»|>|→)$/i, prev:/^(prev(ious)?( month)?|‹|«|<|←)$/i,
    inc:/^(increase|increment|add|plus|more)\b|^\+$/i, dec:/^(decrease|decrement|remove|minus|fewer|less)\b|^[−–-]$/i};
  if (op==='month') { const m=(box.innerText||'').match(M); return m ? [m[1], +m[2]] : null; }
  if (op==='number') { const m=(box.innerText||'').replace(/[+−–-]/g,' ').match(/\b\d{1,3}\b/); return m ? +m[0] : null; }
  if (op in re) { const b=btns.find(x=>re[op].test(nm(x))); if (!b) return false; b.click(); return true; }
  if (op==='day') {
    const [month, day]=arg, full=new RegExp('\\b'+month+'\\s+'+day+'\\b|\\b'+day+'\\s+'+month+'\\b');
    const b=btns.find(x=>full.test(x.getAttribute('aria-label')||x.getAttribute('title')||'')) ||
      btns.find(x=>(x.textContent||'').trim()===String(day) && !x.querySelector('button'));
    if (!b) return false; b.click(); return true;
  }
  return null;
})"""


def parse_date(text, shown=None):
    """(year, month, day) from the text helper's value: 'October 10, 2026', '10 Oct 2026', '2026-10-10', '10/10/2026',
    'October 10' (year: the shown year, or the next one if that month has passed)."""
    import re
    t = (text or "").strip()
    if m := re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", t):
        return int(m[1]), int(m[2]), int(m[3])
    if m := re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", t):
        return int(m[3]), int(m[1]), int(m[2])
    mon = next((i + 1 for i, n in enumerate(MONTHS) if re.search(r"\b" + n[:3], t, re.I)), None)
    if mon is None:
        return None
    nums = [int(x) for x in re.findall(r"\d+", t)]
    day = next((n for n in nums if 1 <= n <= 31), None)
    year = next((n for n in nums if n > 1900), None)
    if day is None:
        return None
    if year is None:
        sy, sm = (shown[1], MONTHS.index(shown[0]) + 1) if shown else (time.localtime().tm_year, time.localtime().tm_mon)
        year = sy if mon >= sm else sy + 1
    return year, mon, day


def run_macro(evaluate, action, text):
    """Drive a calendar to a date / a stepper to a number with its own controls (bounded, verified after each click)."""
    js = lambda op, arg=None: evaluate(f"{MACRO_JS}({action['node']}, {json.dumps(op)}, {json.dumps(arg)})")
    if action["macro"] == "date":
        shown = js("month")
        target = parse_date(text, shown)
        if not shown or not target:
            raise StalePage(f"Calendar value {text!r} could not be set")
        y, m, d = target
        for _ in range(48):
            shown = js("month")
            diff = (y * 12 + m) - (shown[1] * 12 + MONTHS.index(shown[0]) + 1)
            if diff == 0 or not js("next" if diff > 0 else "prev"):
                break
            time.sleep(0.03)
        if not js("day", [MONTHS[m - 1], d]):
            raise StalePage(f"Day {text!r} is not in the calendar")
    else:
        try:
            n = int(str(text).strip().split()[0])
        except (ValueError, IndexError):
            raise StalePage(f"Count {text!r} is not a number")
        for _ in range(40):
            cur = js("number")
            if cur is None or cur == n or not js("inc" if n > cur else "dec"):
                break
            time.sleep(0.03)
        if js("number") != n:
            raise StalePage(f"Could not set the count to {n}")


LAST_SHOT = {}
# 1x1 white JPEG for a session whose very first screenshot failed
BLANK_JPEG = "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA="


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]

    def call(method, **params):
        return cdp(method, session_id=session, **params)

    def evaluate(expression):
        result = call("Runtime.evaluate", expression=expression, returnByValue=True)
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        if kind == "scroll":
            call("Input.dispatchMouseEvent", type="mouseWheel", x=550, y=650, deltaX=0, deltaY=action["delta"])
        elif kind == "key":
            # Enter in the focused text field: submits search boxes and forms that expose no clickable submit
            # control (arXiv's search overlay, most site searches).  Only offered while such a field is focused.
            call("Input.dispatchKeyEvent", type="keyDown", key="Enter", code="Enter", windowsVirtualKeyCode=13,
                 nativeVirtualKeyCode=13, text="\r")
            call("Input.dispatchKeyEvent", type="keyUp", key="Enter", code="Enter", windowsVirtualKeyCode=13,
                 nativeVirtualKeyCode=13)
        elif action.get("macro"):
            run_macro(evaluate, action, request["text"])
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
            target = evaluate("""(action => {
              const e=window.__jevFast?.nodes.get(action.node);
              if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                  !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
              let r=e.getBoundingClientRect();
              if (r.y+r.height/2<0 || r.y+r.height/2>=innerHeight) { e.scrollIntoView({block:'nearest'}); r=e.getBoundingClientRect(); }
              const x=r.x+r.width/2, y=r.y+r.height/2;
              if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
              const hit=document.elementFromPoint(x,y);
              // Covered by one of its own ancestors (an overlay/backdrop painted above its children, e.g. arXiv's
              // search panel): the element is still the observed, visible target, so click it directly.
              const byAncestor=hit && !e.contains(hit) && hit.contains(e);
              if (!byAncestor && !e.contains(hit)) return null;
              if (action.kind==='select') {
                if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                    !o.disabled && !o.closest('optgroup[disabled]'))) return null;
                e.value=action.value;
                e.dispatchEvent(new Event('input',{bubbles:true}));
                e.dispatchEvent(new Event('change',{bubbles:true}));
              }
              if (byAncestor && action.kind==='click') { e.click(); return {x,y,js:true}; }
              return {x,y};
            })(""" + json.dumps(action) + ")")
            if target is None:
                if kind == "select":
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
                raise StalePage("Target changed or is covered. Observe again.")
            if kind != "select" and not target.get("js"):
                x, y = target["x"], target["y"]
                for event in ("mousePressed", "mouseReleased"):
                    call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
                if kind == "fill":
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyDown",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                        commands=["selectAll"],
                    )
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyUp",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                    )
                    call("Input.insertText", text=request["text"])
        return {"executed": action["id"]}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        # a busy page (ads, animations) can hold a frame past the CDP timeout: retry, then fall back to the last frame so
        # a slow screenshot never fails the step (screenshots are for recording / the operator, not for the policy)
        for attempt in range(3):
            try:
                info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72, optimizeForSpeed=True)["data"]
                LAST_SHOT[session] = info["screenshot"]; break
            except Exception:
                if attempt == 2:
                    info["screenshot"] = LAST_SHOT.get(session) or BLANK_JPEG
    return info
