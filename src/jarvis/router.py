"""The turn loop: pick a backend, stream the answer, run tool calls, write the audit record."""

from __future__ import annotations

import time
from typing import Any, AsyncIterator, Callable

from .about import describe as describe_self
from .audit import AuditLog, digest
from .claims import SAID_INSTEAD, Claims
from .closing import SIGN_OFFS, closing
from .llm import Backend, BackendDown
from .manner import Tail, accepted
from .personality import filler, personality
from .refusal import NOTE as REFUSAL_NOTE, Opening, missing
from .tools import ToolRegistry, to_tool_message

# The rules that hold whatever tools there are. Who Jarvis is comes before them (jarvis.personality), and what it
# can and cannot do comes after them, built from the tools that are really there (jarvis.about).
RULES = (
    "About the lab and the machine you run on, you know only what your tools return. "
    "Use a tool whenever the question is about something one of your tools can read. "
    "Call only a tool whose description covers what was asked; do not call a different tool instead, and do not "
    "describe anything in the lab that you have not read from a tool. When the owner asks again or asks for more detail "
    "about something in the lab, call the tool again instead of answering from what you said before. "
    "If the tool result does not hold what was asked, say what it does show and that you cannot see more; "
    "never guess a cause, a detail or a number. "
    "Everything else, such as conversation, general knowledge, explanations, advice, writing or a story, you answer "
    "yourself, fully and well; no tool is needed for that, and the lack of one is no reason to decline. "
    "You are the one with the tools: never ask the owner what happened in the lab or what might have caused something. "
    "Ask a question only when a request cannot be carried out without one more detail from them, such as which of two nodes. "
    "Never ask whether there is anything else. Never offer to do something: either do it or say nothing about it. "
    "You cannot change how you behave from inside a conversation."
)
SYSTEM_PROMPT = personality() + "\n\n" + RULES
# Added to the request itself for a voice turn, so the system prompt (and the model's cache of it) never changes.
SPOKEN_NOTE = (
    " (This request was spoken and your reply will be read aloud. Call your tools exactly as you would for a "
    "typed question. Once you have the facts, reply in one to three short sentences and lead with the answer. "
    "The words were transcribed from speech, so read an odd word as the nearest thing in this lab, for example "
    "Proxmox, OPNsense, PiHole or a node name. "
    "End with a question only when you need something from the person to carry on; do not ask whether there is "
    "anything else.)"
)
# Added to a bare "yes", so the model acts on what it asked instead of asking again.
YES_NOTE = (
    ' (The owner is saying yes to your last question: "{question}" Carry it out now with a tool call. If no tool of '
    "yours can do it, say so in one sentence and promise nothing.)"
)


MAX_RECHECKS = 1  # how often in one turn the model is sent back for claiming what no tool reported
MAX_REFUSALS = 1  # how often in one turn the model is sent back for a bare refusal


class Router:
    def __init__(
        self,
        backends: list[Backend],
        tools: ToolRegistry,
        audit: AuditLog,
        max_tool_rounds: int = 8,
        max_history: int = 40,
        audit_text: bool = False,
        context: Callable[[], str] | None = None,
        on_turn: Callable[[dict[str, Any]], None] | None = None,
        skill_for: Callable[[str], tuple[str, str] | None] | None = None,
        describe: Callable[[list[str], str], str] = describe_self,
    ):
        self.backends = backends
        self.tools = tools
        self.audit = audit
        self.max_tool_rounds = max_tool_rounds
        self.max_history = max_history
        self.audit_text = audit_text
        self._context = context
        self._on_turn = on_turn
        self._skill_for = skill_for
        self._describe = describe  # what Jarvis is and can do, in words; see jarvis.about
        self._sessions: dict[str, list[dict[str, Any]]] = {}

    def _system(self, backend: Backend) -> str:
        """The system prompt for one backend. Notes from the vault go to the local model only."""
        if backend.name != "local":
            return SYSTEM_PROMPT
        # What Jarvis is and can do, from the tools that are really there.
        parts = [SYSTEM_PROMPT, self._describe(self.tools.names(), backend.model)]
        extra = self._context() if self._context is not None else ""
        if extra:
            parts.append(extra)
        return "\n\n".join(parts)

    def _history(self, session: str) -> list[dict[str, Any]]:
        return self._sessions.setdefault(session, [])

    def forget(self, session: str) -> None:
        """A conversation begun anew: nothing of the one before is sent to the model again."""
        self._sessions.pop(session, None)

    def _forget(self, session: str, messages: list[dict[str, Any]]) -> None:
        """Takes one turn's own messages out of the conversation: those and no others, wherever they are by now."""
        history = self._sessions.get(session)
        if history:
            history[:] = [m for m in history if not any(m is mine for mine in messages)]

    def _trim(self, session: str) -> None:
        hist = self._sessions[session]
        if len(hist) > self.max_history:
            hist = hist[-self.max_history :]
            while hist and hist[0]["role"] != "user":
                hist.pop(0)
            self._sessions[session] = hist

    def _journal(self, session: str, user: str, spoken: bool, user_text: str, reply: str, tools: list[str]) -> None:
        if self._on_turn is not None:
            try:  # a problem in the journal must not cost the person their answer
                self._on_turn({"ts": time.time(), "session": session, "user": user, "spoken": spoken,
                               "user_text": user_text, "reply": reply, "tools": tools})
            except Exception:
                pass

    async def turn(
        self, session: str, user_text: str, user: str = "local", spoken: bool = False
    ) -> AsyncIterator[dict[str, Any]]:
        started = time.monotonic()
        history = self._history(session)
        mark = len(history)
        own: list[dict[str, Any]] = []  # every message this turn puts into the conversation

        def keep(*messages: dict[str, Any]) -> None:
            history.extend(messages)
            own.extend(messages)

        keep({"role": "user", "content": user_text})
        reply_parts: list[str] = []
        tool_names: list[str] = []
        stats: dict[str, Any] = {}
        used: Backend | None = None
        rounds = 0
        failures: list[str] = []
        finished = False
        claims = Claims(self.tools.names())  # what the tools report in this turn; see jarvis.claims
        tail = Tail(reject=claims.unbacked)  # holds back a closing offer and any claim with nothing behind it
        trimmed = ""
        rechecks = 0
        sent_back = 0  # bare refusals returned to the model in this turn
        refusals: list[str] = []
        checks: list[dict[str, Any]] = []  # the model's refused draft and the note about it; never kept
        carried = ""  # what was already shown of a draft that was sent back
        # "No, I'm all set": a short sign-off, with no lookup and no further question.
        previous = history[mark - 1] if mark else {}
        # A bare "yes" to a yes-or-no question is sent on with the question it answers, and is never a goodbye.
        yes_to = accepted(previous.get("content") or "", user_text) if previous.get("role") == "assistant" else None
        if previous.get("role") == "assistant" and yes_to is None:
            local = next((b for b in self.backends if b.name == "local"), None)
            try:
                how = await closing(local, previous.get("content") or "", user_text)
            except BaseException:  # cancelled while the model was judging: leave no half turn behind
                self._forget(session, own)
                raise
            if how is not None:
                # The turn is recorded before it is said, so a client that leaves now still leaves a whole turn.
                reply = SIGN_OFFS[(mark // 2) % len(SIGN_OFFS)]
                keep({"role": "assistant", "content": reply})
                record = {
                    "session": session, "user": user, "backend": None, "model": None, "rounds": 0, "tools": [],
                    "fallback_from": [], "closing": how,
                    "user_chars": len(user_text), "user_digest": digest(user_text),
                    "reply_chars": len(reply), "reply_digest": digest(reply),
                    "ms": round((time.monotonic() - started) * 1000),
                }
                if spoken:
                    record["spoken"] = True
                if self.audit_text:
                    record["user_text"] = user_text
                    record["reply_text"] = reply
                rec = self.audit.append("turn", record)
                self._journal(session, user, spoken, user_text, reply, [])
                self._trim(session)
                yield {"type": "token", "text": reply}
                yield {"type": "done", "audit_seq": rec["seq"], "closing": how}
                return
        # A learned procedure for this kind of request rides along with it, for the local model only.
        skill = self._skill_for(user_text) if self._skill_for is not None else None

        try:
            while True:
                rounds += 1
                messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history]
                asked = user_text + (YES_NOTE.format(question=yes_to) if yes_to and rounds == 1 else "") + (SPOKEN_NOTE if spoken else "")
                # The notes ride on this turn's request only; the stored history keeps the plain words.
                messages[mark + 1] = {"role": "user", "content": asked}
                calls: list[dict[str, Any]] = []
                raw_calls: list[Any] = []
                round_text: list[str] = []
                opening = Opening()  # holds a reply that starts with a refusal until it can be judged whole
                candidates = [used] if used else self.backends
                answered = False
                # After the last allowed tool round the model gets no tools, so it must answer with what it has.
                offered = self.tools.schemas() if rounds <= self.max_tool_rounds else []
                for backend in candidates:
                    messages[0] = {"role": "system", "content": self._system(backend)}
                    if skill is not None and backend.name == "local":
                        messages[mark + 1] = {"role": "user", "content": f"{asked}\n\n(Follow your skill \"{skill[0]}\" for this request:\n{skill[1]})"}
                    else:
                        messages[mark + 1] = {"role": "user", "content": asked}
                    try:
                        async for event in backend.chat(messages, offered):
                            if not answered:
                                answered = True
                                if used is None:
                                    used = backend
                                    yield {"type": "route", "backend": backend.name, "model": backend.model}
                            if event["type"] == "text":
                                shown = tail.feed(opening.feed(event["text"]))
                                if shown:
                                    round_text.append(shown)
                                    yield {"type": "token", "text": shown}
                            elif event["type"] == "tool_calls":
                                calls.extend(event["calls"])
                                raw_calls.extend(event["raw"])
                            elif event["type"] == "done":
                                stats = {k: v for k, v in event.items() if k != "type"}
                        break
                    except BackendDown as exc:
                        if answered:
                            raise
                        failures.append(f"{backend.name}: {exc}")
                        continue
                if not answered:
                    raise BackendDown("; ".join(failures) or "no model backend is configured")

                held = opening.held()
                # A draft written in answer to the claims check is not judged again: that check told the model to
                # say that it cannot, and one turn sends the model back once for each reason at most.
                lacks = missing(held) if held and not calls and not rechecks else None
                # Only while a round with tools is still to come: the note invites a tool call.
                if lacks is not None and sent_back < MAX_REFUSALS and rounds < self.max_tool_rounds:
                    # A bare no. Nothing of it was shown; the model is told what is missing and gets one more
                    # try, in which it can call a tool after all or refuse with a reason and a way forward.
                    sent_back += 1
                    draft = {"role": "assistant", "content": held.strip()}
                    note = {"role": "user", "content": REFUSAL_NOTE.format(missing=lacks)}
                    keep(draft, note)
                    checks.extend((draft, note))
                    tail = Tail(reject=claims.unbacked)
                    continue
                if held:
                    shown = tail.feed(opening.take())
                    if shown:
                        round_text.append(shown)
                        yield {"type": "token", "text": shown}
                # Text that leads into a tool call is passed on whole; only the end of the reply is trimmed.
                rest, trimmed = (tail.release(), "") if calls else tail.finish()
                if rest:
                    round_text.append(rest)
                    yield {"type": "token", "text": rest}
                text = "".join(round_text)
                if calls and tail.dropped is not None:
                    # A claim made before the tools have even run: it is left out, and the tools' results decide
                    # what the reply after them may claim.
                    refusals.append(tail.dropped[1])
                    tail.dropped = None
                refused = None if calls else tail.refused
                if refused is not None:
                    refusals.append(refused[1])
                    if rechecks < MAX_RECHECKS and rounds <= self.max_tool_rounds:
                        # The draft claimed something no tool reported. It was not shown; the model is told so and
                        # gets one more try, in which it can call the tool or say that it cannot.
                        rechecks += 1
                        reply_parts.append(text)
                        carried += text
                        draft = {"role": "assistant", "content": (text + refused[0]).strip()}
                        note = {"role": "user", "content": claims.note(refused[1], refused[0])}
                        keep(draft, note)
                        checks.extend((draft, note))
                        tail = Tail(reject=claims.unbacked)
                        continue
                    said = SAID_INSTEAD[refused[1]]
                    if (carried + text).strip() and not (carried + text).endswith((" ", "\n")):
                        said = " " + said
                    text += said
                    yield {"type": "token", "text": said}
                reply_parts.append(text)
                if not calls:
                    history[:] = [m for m in history if not any(m is c for c in checks)]
                    keep({"role": "assistant", "content": carried + text})
                    break
                if rounds > self.max_tool_rounds:
                    history[:] = [m for m in history if not any(m is c for c in checks)]
                    note = "I stopped because the tool-call limit for one turn was reached."
                    keep({"role": "assistant", "content": (carried + text + " " + note).strip()})
                    reply_parts.append(note)
                    yield {"type": "token", "text": note}
                    break
                keep({"role": "assistant", "content": text, "tool_calls": raw_calls})
                # Said before the tools run, so a voice client can acknowledge while it waits.
                yield {"type": "working", "tools": [call["name"] for call in calls], "say": filler(mark // 2 + rounds)}
                for call in calls:
                    result = await self.tools.dispatch(session, call["name"], call["arguments"])
                    claims.tool(call["name"], result)
                    tool_names.append(call["name"])
                    keep(to_tool_message(call["name"], result))
                    yield {
                        "type": "tool",
                        "name": call["name"],
                        "arguments": call["arguments"],
                        "ok": "error" not in result,
                    }
        except Exception as exc:
            # Roll the failed turn out of the history so the next one starts clean.
            self._forget(session, own)
            message = str(exc) if isinstance(exc, BackendDown) else f"{type(exc).__name__}: {exc}"
            self.audit.append(
                "turn_failed",
                {
                    "session": session,
                    "backend": used.name if used else None,
                    "error": message[:300],
                    "ms": round((time.monotonic() - started) * 1000),
                },
            )
            finished = True
            yield {"type": "error", "message": message}
            return
        else:
            finished = True
        finally:
            if not finished:
                # The client went away mid-turn. Leave no half turn behind.
                self._forget(session, own)
                self.audit.append(
                    "turn_aborted",
                    {"session": session, "backend": used.name if used else None,
                     "ms": round((time.monotonic() - started) * 1000)},
                )

        reply = "".join(reply_parts)
        record: dict[str, Any] = {
            "session": session,
            "user": user,
            "backend": used.name,
            "model": used.model,
            "rounds": rounds,
            "tools": tool_names,
            "fallback_from": failures,
            "user_chars": len(user_text),
            "user_digest": digest(user_text),
            "reply_chars": len(reply),
            "reply_digest": digest(reply),
            "ms": round((time.monotonic() - started) * 1000),
            **stats,
        }
        if spoken:
            record["spoken"] = True
        if skill is not None and used.name == "local":
            record["skill"] = skill[0]
        if trimmed:
            record["trimmed_chars"] = len(trimmed)
        if refusals:
            record["claims_refused"] = refusals
        if sent_back:
            record["refusals_sent_back"] = sent_back
        if self.audit_text:
            record["user_text"] = user_text
            record["reply_text"] = reply
        rec = self.audit.append("turn", record)
        self._journal(session, user, spoken, user_text, reply, tool_names)
        self._trim(session)
        done = {"type": "done", "audit_seq": rec["seq"], "backend": used.name, "model": used.model, **stats}
        if sent_back:
            done["refusals_sent_back"] = sent_back
        if trimmed:
            done["trimmed"] = trimmed
        if refusals:
            done["claims_refused"] = refusals
        yield done
