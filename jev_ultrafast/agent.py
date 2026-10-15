"""The complete agent loop. Typed choices, observable state, bounded execution."""

import base64
import json
import os
import re
import time
from pathlib import Path

from .browser import Browser, StalePage
from .model import action_space, choose, field_context, field_text, select_option, subgoal_satisfied
from .questions import MAX_STEPS, PLAN
from .model import post_json


def make_plan(task):
    """Split a task into ordered sub-goals with the text model (one call per task).  Falls back to [task]."""
    key = os.environ.get("TEXT_MODEL_API_KEY")
    if not key:
        return [task]
    base = os.environ.get("TEXT_MODEL_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    extra = json.loads(os.environ["TEXT_MODEL_EXTRA_JSON"]) if os.environ.get("TEXT_MODEL_EXTRA_JSON") else {}
    try:
        r = post_json(base + "/chat/completions", key, {
            "model": os.environ.get("TEXT_MODEL", "deepseek-chat"), "max_tokens": 400, "temperature": 0,
            "response_format": {"type": "json_object"}, **extra,
            "messages": [{"role": "system", "content": PLAN}, {"role": "user", "content": task}]})
        steps = json.loads(r["choices"][0]["message"]["content"])["steps"]
        steps = [s.strip() for s in steps if isinstance(s, str) and s.strip()][:8]
        return steps or [task]
    except Exception:
        return [task]


REPEATABLE = re.compile(r"\b(next|previous|prev|more|load more|show more|page|scroll|increase|decrease|add|remove one|plus|minus)\b|^[0-9]+$|^[+−-]$", re.I)
UNSAFE = re.compile(r"\b(place (your )?order|pay( now)?|purchase|buy now|complete (purchase|booking|order)|confirm (purchase|booking|order|payment)|"
                    r"checkout|send( message| gift| e-?gift)?|submit (request|application|order|payment)|apply now|donate|contact (seller|agent|owner))\b", re.I)


class Agent:
    def __init__(self, url, goals, *, record_dir=None, screenshots=False):
        task = goals.strip() if isinstance(goals, str) else "\n".join(goals).strip()
        if not task:
            raise ValueError("Supply a task")
        plan = make_plan(task) if os.environ.get("JEV_PLANNER") == "1" else [task]
        self.pending_text = None
        self.browser = Browser(url)
        self.record_dir = Path(record_dir) if record_dir else None
        self.screenshots = screenshots or bool(record_dir)
        try:
            page = self.browser.observe(screenshot=self.screenshots)
        except Exception:
            self.browser.close()
            raise
        self.state = dict(
            browser=self.browser,
            goal=plan[0],
            task=task,
            page=page,
            decision=None,
            history=[],
            status="ready",
            plan=plan,
            plan_index=0,
            decisions=[],
            text_calls=[],
            elapsed_ms=0,
            started_at=None,
            record=bool(self.record_dir),
        )
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            (self.record_dir / "000000.jpg").write_bytes(base64.b64decode(page["screenshot"]))

    def _observe(self, tries=8):
        """Observe, waiting out an in-flight navigation (the page fires StalePage while the document loads)."""
        for i in range(tries):
            try:
                return self.state["browser"].observe(screenshot=self.screenshots)
            except StalePage:
                if i == tries - 1:
                    raise
                time.sleep(0.5)

    def _decision_page(self):
        """The page the model decides on: excluded (repeatedly unexecutable) actions are hidden for this decision.
        JEV_SAFE=1 (evaluation on live third-party sites): controls that commit something irreversible (pay, order,
        send a message, submit an application / request) are never offered."""
        page = self.state["page"]
        excluded = self.state.get("excluded") or set()
        safe = os.environ.get("JEV_SAFE") == "1"
        undo = self._undo_guard()
        if not excluded and not safe and not undo:
            return page
        return {**page, "actions": [a for a in page["actions"] if a["id"] not in excluded and not (safe and UNSAFE.search(a.get("label", "")))
                                    and not undo(a)]}

    def _undo_guard(self, window=3):
        """For a few steps after a click / select that changed the page, do not offer the same control again (or its
        "remove this filter" chip, or another option of the same dropdown): re-clicking an applied filter, sort or radio
        undoes it -- the agent toggled filters on and off until its step budget ran out.  Repeating is fine for
        pagination / load-more / scroll / +- controls."""
        recent = [h for h in self.state["history"][-window:] if h.get("page_changed") and h["kind"] in ("click", "select")]
        if not recent:
            return None
        norm = lambda t: re.sub(r"[^a-z0-9]+", " ", str(t or "").lower()).strip()
        labels = {norm(h["action"].split(" → ")[0] if h["kind"] == "select" else h["action"]) for h in recent}
        labels = {l for l in labels if l and not REPEATABLE.search(l)}
        if not labels:
            return None

        def blocked(a):
            if a["kind"] not in ("click", "select"):
                return False
            lab = norm(a["label"].split(" → ")[0] if a["kind"] == "select" else a["label"])
            return bool(lab) and any(l == lab or (len(l) >= 4 and l in lab) for l in labels)
        return blocked

    def _escalation_reason(self, ops_off):
        """JEV_ESCALATE=1: hand this step to System 2 when the fast policy is visibly failing -- its DONE was just
        rejected, or its recent actions changed nothing / keep repeating."""
        if os.environ.get("JEV_ESCALATE") != "1":
            return None
        if ops_off:
            return "done_rejected"
        h = self.state["history"]
        if self.state.get("excluded") or (len(h) >= 2 and not h[-1]["page_changed"] and not h[-2]["page_changed"]):
            return "stuck"
        if len(h) >= 3 and h[-1]["choice"] == h[-3]["choice"] and h[-1]["url"] == h[-3]["url"]:
            return "stuck"
        return None

    def snapshot(self):
        return {
            **{k: v for k, v in self.state.items() if k != "browser"},
            "elements": action_space(self.state["page"]["actions"])[0],
        }

    def command(self, name, body=None):
        body = body or {}
        state = self.state
        if name == "tick":
            choice = None
            try:
                self.command("predict", {})
                choice = state["decision"]["choice"] if state["decision"] else None   # act() consumes the decision
                return self.command("act", {"fingerprint": state["page"]["fingerprint"]})
            except (StalePage, ValueError) as e:
                if not isinstance(e, StalePage) and "Text helper" not in str(e):
                    raise
                # A decision that could not be executed (target covered / page changed under it / the text helper
                # returned nothing usable) is retried after a fresh observation.  The same choice failing three
                # times in a row is excluded from the next decisions instead of looping until the budget is gone.
                if choice and choice == state.get("_stale_choice"):
                    state["_stale_count"] = state.get("_stale_count", 0) + 1
                else:
                    state["_stale_choice"], state["_stale_count"] = choice, 1
                if choice and state["_stale_count"] >= 3:
                    state.setdefault("excluded", set()).add(choice)
                    state["_stale_count"] = 0
                state["decision"] = None
                state["status"] = "ready"
                state["page"] = self._observe()
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
        elif name == "predict":
            if not state["browser"]:
                raise ValueError("Start a demo first")
            if state["started_at"] is None:
                state["started_at"] = time.perf_counter()
            if not state["browser"].fresh(state["page"]):
                state["page"] = self._observe()
            state["decision"] = None
            if state["status"] in {"done", "blocked"}:
                raise ValueError("This run has stopped. Start a fresh demo.")
            if len(state["decisions"]) >= MAX_STEPS * 2:
                raise ValueError("Reached the demo's model-call budget")
            ops_off = ("DONE",) if state.get("_done_rejected_at") == state["page"]["fingerprint"] else ()
            state["decision"] = choose(self._decision_page(), state["goal"], state["history"], exclude_ops=ops_off,
                                       escalate=self._escalation_reason(ops_off))
            if state["decision"]["raw_answers"]["operation"].get("system2"):
                state["escalations"] = state.get("escalations", 0) + 1
            state["decisions"].append(
                {
                    **state["decision"],
                    "fingerprint": state["page"]["fingerprint"],
                    "elapsed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                }
            )
            state["status"] = "predicted"
        elif name == "act":
            decision, page = state["decision"], state["page"]
            if not decision or body.get("fingerprint") != page["fingerprint"]:
                raise ValueError("Observe and choose before acting")
            # Consume once, before any mutation or model call. A retry cannot double-click.
            state["decision"] = None
            selected = decision["choice"]
            if selected in {"DONE", "BLOCKED"}:
                if not state["browser"].fresh(page):
                    state["status"] = "ready"
                    raise StalePage("Page changed since the decision. Choose again.")
                # JEV_VERIFY_DONE=1: the text model also checks the WHOLE task before a final DONE is accepted (the policy's
                # most common real-site failure is DONE right after the search, before filters / opening the result)
                verify = len(state["plan"]) > 1 or os.environ.get("JEV_VERIFY_DONE") == "1"
                # JEV_NOUL_DONE=1: the policy's own completion head must agree before a DONE is accepted (no LLM call)
                noul_no = (selected == "DONE" and decision.get("goal_done") is not None
                           and decision["goal_done"] < float(os.environ.get("JEV_NOUL_TAU", "0.5")))
                if noul_no:
                    state["noul_rejections"] = state.get("noul_rejections", 0) + 1
                if selected == "DONE" and (noul_no or (verify and subgoal_satisfied(state["goal"], state["task"], page) is False)):
                    # the sub-goal checker sees it is not satisfied yet: decide again on this page without DONE
                    state["_done_rejected_at"] = page["fingerprint"]
                    state["status"] = "ready"
                    state["done_rejections"] = state.get("done_rejections", 0) + 1
                    return self.snapshot()
                if selected == "DONE" and state["plan_index"] + 1 < len(state["plan"]):
                    # sub-goal satisfied: move on to the next one instead of finishing the task
                    state["plan_index"] += 1
                    state["goal"] = state["plan"][state["plan_index"]]
                    state["status"] = "ready"
                    state["subgoals_done"] = state.get("subgoals_done", 0) + 1
                    state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                    return self.snapshot()
                state["status"] = "done" if selected == "DONE" else "blocked"
                state["plan_index"] = len(state["plan"]) if selected == "DONE" else state["plan_index"]
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
            action = next(a for a in page["actions"] if a["id"] == selected)
            if action["kind"] == "select":
                # the policy chose the dropdown; the text model reads which of its options the goal asks for
                picked = select_option(state["goal"] if state["goal"] == state["task"] else state["task"] + "\nCurrent step: " + state["goal"], action, page)
                if picked is not None and picked["id"] != selected:
                    state["select_overrides"] = state.get("select_overrides", 0) + 1
                    action, selected = picked, picked["id"]
            if len(state["history"]) >= MAX_STEPS:
                state["status"] = "blocked"
                raise ValueError(f"Stopped at the {MAX_STEPS}-action demo budget")
            text, helper = None, None
            if action["kind"] == "fill":
                if not state["browser"].fresh(page):
                    raise StalePage("Page changed before text generation. Choose again.")
                context = field_context(state["goal"], action, page, state["history"])
                if self.pending_text and self.pending_text[0] == context:
                    _, text, helper = self.pending_text
                else:
                    text, helper = field_text(context)
                    self.pending_text = (context, text, helper)
                    state["text_calls"].append({**helper, "field": action["label"], "value": text})
            # Browser.act checks freshness immediately before input, including after text generation.
            state["browser"].act(action, page, text=text)
            self.pending_text = None
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            # Record execution before observing. A stale post-action observation must not erase the action.
            state["history"].append(
                {
                    "step": len(state["history"]) + 1,
                    "action": action["label"],
                    "kind": action["kind"],
                    "choice": selected,
                    "probability": decision["probabilities"][selected],
                    "confidence": decision["confidence"],
                    "latency_ms": decision["latency_ms"],
                    "text": text,
                    "text_helper": helper["model"] if helper else None,
                    "text_latency_ms": helper["latency_ms"] if helper else 0,
                    "operation": decision["operation"],
                    "target": decision["target"],
                    "page_changed": None,
                    "url": page["url"],
                    "usage": decision["usage"],
                    "executed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                    "elapsed_ms": state["elapsed_ms"],
                }
            )
            state["page"] = self._observe()
            changed = state["page"]["fingerprint"] != page["fingerprint"]
            prev = state["history"][-2] if len(state["history"]) >= 2 else None
            if changed:
                state.pop("excluded", None)      # progress resets the exclusions
            elif prev and prev["choice"] == selected and prev["page_changed"] is False:
                # the same action, executed twice, changed nothing: stop offering it (stall guard)
                state.setdefault("excluded", set()).add(selected)
            recent = state["history"][-4:]
            if (sum(h["choice"] == selected for h in recent) >= 3 and len({h["url"] for h in recent}) == 1
                    and state["page"]["url"] == page["url"]):
                # the same action again and again on the same URL (e.g. a logo link that reloads the page): exclude it
                state.setdefault("excluded", set()).add(selected)
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            state["history"][-1].update(
                page_changed=state["page"]["fingerprint"] != page["fingerprint"],
                url=state["page"]["url"],
                title=state["page"].get("title", ""),
                elapsed_ms=state["elapsed_ms"],
            )
            if state["record"]:
                (self.record_dir / f"{state['elapsed_ms']:06d}.jpg").write_bytes(
                    base64.b64decode(state["page"]["screenshot"])
                )
            repeated = state["history"][-3:]
            state["status"] = (
                "blocked"
                if len(repeated) == 3 and all(h["page_changed"] is False and h["kind"] != "wait" for h in repeated)
                else "ready"
            )
        else:
            raise ValueError("Unknown command")
        return self.snapshot()

    def run(self):
        while self.state["status"] not in {"done", "blocked"}:
            yield self.command("tick")

    def close(self):
        self.browser.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
