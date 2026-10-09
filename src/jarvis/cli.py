"""The jarvis command.

  jarvis                     talk to Jarvis in this terminal
  jarvis chat "question"     one question, one answer (the answer on standard output, notes on standard error)
  jarvis doctor              check that Jarvis is in working order; asks the model one question
  jarvis doctor --quick      the same without asking the model anything
  jarvis audit               check that nothing in the audit log was changed or removed
  jarvis serve               the service the panel on the desktop talks to (systemd starts it)
  jarvis --version

A conversation in the terminal runs inside this command and lasts as long as the command does; it is not
the conversation in the panel. Both write to the same audit log.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import getpass
import os
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, TextIO

import httpx

from .audit import AuditDamaged, verify
from .config import Settings, SettingsError
from .core import Jarvis

SURFACE = "The owner is typing to you in a terminal on that machine and reads your answers there."
DOCTOR_QUESTION = "This is a self-test. Reply with the one word: ready."



def _cluster(reading: dict) -> str:
    nodes = reading.get("nodes") or []
    online = sum(1 for node in nodes if node.get("online"))
    return f"{online} of {len(nodes)} nodes online, {'quorate' if reading.get('quorate') else 'NOT quorate'}"


def _datastores(reading: dict) -> str:
    stores = reading.get("datastores") or []
    used = [store.get("used_percent") for store in stores if isinstance(store.get("used_percent"), (int, float))]
    fullest = f", the fullest {max(used)} percent used" if used else ""
    return f"{len(stores)} datastore{'s' if len(stores) != 1 else ''}{fullest}"


def _firewall(reading: dict) -> str:
    return f"{reading.get('name') or 'the firewall'} answers" + (f", up {reading['uptime']}" if reading.get("uptime") else "")


def _resolvers(reading: dict) -> str:
    rows = reading.get("resolvers") or []
    healthy = sum(1 for row in rows if row.get("healthy"))
    return f"{healthy} of {len(rows)} resolvers healthy" + ("" if healthy == len(rows) else " (ask Jarvis which and why)")


def _switch(reading: dict) -> str:
    model = (reading.get("system") or {}).get("model") or "the switch"
    return f"{model} answers, {reading.get('ports_up', 0)} ports up"


# What the doctor asks each system, with the same read-only tool the model uses, and how it says what it heard.
LAB_CHECKS = {
    "proxmox": ("proxmox_cluster_status", _cluster),
    "pbs": ("pbs_datastores", _datastores),
    "opnsense": ("opnsense_status", _firewall),
    "dns": ("dns_health", _resolvers),
    "switch": ("switch_status", _switch),
}

async def turn(jarvis: Jarvis, session: str, text: str, out: TextIO, notes: TextIO, spoken: bool = False,
               user: str = "local") -> dict[str, Any]:
    """One turn, written as it comes. Returns the last event: "done", or "error" with its message."""
    open_line = False
    last: dict[str, Any] = {"type": "error", "message": "the turn ended without an answer"}

    def note(words: str) -> None:
        nonlocal open_line
        if open_line:
            out.write("\n")
            out.flush()
            open_line = False
        print(words, file=notes, flush=True)

    # Closed here whatever ends this turn, a Ctrl-C in the middle of writing included, so that the router
    # takes a half turn out of the conversation now and not at some later moment.
    async with contextlib.aclosing(jarvis.router.turn(session, text, user=user, spoken=spoken)) as events:
        async for event in events:
            kind = event["type"]
            if kind == "token":
                out.write(event["text"])
                out.flush()
                open_line = not event["text"].endswith("\n")
            elif kind == "working":
                note(f"[{event['say']} {', '.join(event['tools'])}]")
            elif kind == "tool" and not event["ok"]:
                note(f"[{event['name']} failed]")
            elif kind == "error":
                note(f"jarvis: {event['message']}")
                last = event
            elif kind == "done":
                if open_line:
                    out.write("\n")
                    out.flush()
                    open_line = False
                speed = event.get("tokens_per_second")
                who = f"{event['model']}" if event.get("model") else "no model call"
                note(f"[{who}, audit record {event['audit_seq']}{f', {speed} tokens a second' if speed else ''}]")
                last = event
    return last


def _run(loop: asyncio.AbstractEventLoop, coro: Any) -> Any:
    """Runs to the end; on Ctrl-C the turn is cancelled properly, so it leaves no half turn behind."""
    task = loop.create_task(coro)
    try:
        return loop.run_until_complete(task)
    except KeyboardInterrupt:
        if not task.done():  # a turn the Ctrl-C itself ended is over already, and must not be waited for
            task.cancel()
            with contextlib.suppress(BaseException):
                loop.run_until_complete(task)
        raise


def _finish(loop: asyncio.AbstractEventLoop, jarvis: Jarvis) -> None:
    """Ends the loop the way asyncio.run ends its own: whatever a turn left for the loop to close (the model's
    stream is closed that way) is run to its end first."""
    loop.run_until_complete(jarvis.close())
    loop.run_until_complete(asyncio.sleep(0))
    left = asyncio.all_tasks(loop)
    if left:
        loop.run_until_complete(asyncio.gather(*left, return_exceptions=True))
    loop.run_until_complete(loop.shutdown_asyncgens())
    loop.close()


def chat(settings: Settings, words: list[str], spoken: bool, client: httpx.AsyncClient | None = None) -> int:
    if not settings.has_model:
        print(f"jarvis: no local model is set (JARVIS_LOCAL_MODEL=none in {settings.site}), so there is nothing to "
              "answer with. Name a model there and run: update --repair", file=sys.stderr)
        return 1
    loop = asyncio.new_event_loop()
    jarvis = Jarvis(settings, client, SURFACE)
    try:
        if words:
            try:
                last = _run(loop, turn(jarvis, "terminal", " ".join(words), sys.stdout, sys.stderr, spoken))
            except KeyboardInterrupt:
                return 130
            return 0 if last["type"] == "done" else 1
        with contextlib.suppress(ImportError):
            import readline  # noqa: F401 (line editing at the prompt)
        print("Jarvis. An empty line, Ctrl-D or Ctrl-C leaves.", file=sys.stderr)
        while True:
            try:
                said = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print(file=sys.stderr)
                return 0
            if not said:
                return 0
            try:
                _run(loop, turn(jarvis, "terminal", said, sys.stdout, sys.stderr, spoken))
            except KeyboardInterrupt:
                print("\n[stopped]", file=sys.stderr)
    finally:
        _finish(loop, jarvis)


def _named(model: str) -> str:
    return model if ":" in model else model + ":latest"  # a model without a tag is the one Ollama calls latest


async def doctor(settings: Settings, quick: bool, out: TextIO, client: httpx.AsyncClient | None = None,
                 release_check: bool = False) -> int:
    """Looks at every part and says what it finds. With release_check (the installer's use), what is wrong
    with the log Jarvis itself keeps is said as a warning and does not count: the release is not at fault."""
    faults = 0

    def ok(words: str) -> None:
        print(f"   ok: {words}", file=out, flush=True)

    def fault(words: str) -> None:
        nonlocal faults
        faults += 1
        print(f"   FAULT: {words}", file=out, flush=True)

    def warn(words: str) -> None:
        print(f"   WARNING: {words}", file=out, flush=True)

    def log_fault(words: str) -> None:
        if release_check:
            print(f"   WARNING: {words}", file=out, flush=True)
        else:
            fault(words)

    release = "(a release is being installed)" if settings.installing else settings.release or "(not installed from a release)"
    ok(f"Jarvis {release}; settings from {settings.site}")
    path = settings.audit_path
    scratch: tempfile.TemporaryDirectory | None = None

    def with_scratch_log() -> Jarvis:
        """The rest is still looked at, with a log that is thrown away afterwards."""
        nonlocal scratch
        scratch = tempfile.TemporaryDirectory(prefix="jarvis-doctor-")
        return Jarvis(dataclasses.replace(settings, data_dir=Path(scratch.name)), client, SURFACE)

    try:
        jarvis = Jarvis(settings, client, SURFACE)
        if path.exists():
            good, count, message = verify(path)
            (ok if good else log_fault)(f"audit log {path}: {count} records, {message}")
        else:
            ok(f"audit log {path}: no records yet")
        if not os.access(path if path.exists() else path.parent, os.W_OK):
            fault(f"the audit log {path} cannot be written by the user {getpass.getuser()}")
            jarvis = with_scratch_log()
    except AuditDamaged as exc:
        log_fault(f"audit log: {exc}. Jarvis will not add to it: look at it, then move it away to start a new one")
        jarvis = with_scratch_log()
    except OSError as exc:
        fault(f"the audit log cannot be kept at {path} ({type(exc).__name__}; this is the user {getpass.getuser()})")
        return 1
    try:
        status = await jarvis.tools.dispatch("doctor", "local_status", {}, audit=False)
        if "error" in status:
            fault(f"the tool local_status fails: {status['error']}")
        else:
            gpu = "a GPU" if status["gpu"].get("available") else "no GPU"
            ok(f"tools: local_status reads this machine ({status['host']}, {gpu})")
        # The vault and the lab's systems. A locked vault is the normal state after a restart; a system that does
        # not answer is not the fault of a release, so for the installer neither counts.
        vault = jarvis.vault
        if vault.status == "unlocked":
            ok(f"vault: unlocked, {vault.detail}")
        elif vault.status == "locked":
            warn("vault: locked since the last restart; Jarvis cannot read the lab. As root on the brain, run: jarvis-unlock")
        elif vault.status == "error":
            (warn if release_check else fault)(f"vault: {vault.detail}")
        elif settings.secrets_mode == "sops":
            warn(f"vault: {vault.detail}; Jarvis has no tools for the lab until one is there")
        for system, why in jarvis.lab.items():
            if why and vault.status == "unlocked":
                warn(f"{system}: no tools ({why})")
            elif not why and system == "notes":
                await jarvis.notes.start()
                if not jarvis.notes.ready:
                    (warn if release_check else fault)(f"notes: not available: {jarvis.notes.detail}")
                    continue
                found = await jarvis.tools.dispatch("doctor", "notes_search", {"query": "notes"}, audit=False)
                how = found.get("search", "")
                line = f"notes: {jarvis.notes.detail}, {len(jarvis.notes.notes())} notes; search by {how}"
                fine = (how == "words and meaning" or settings.embed_model == "none") and not jarvis.notes.detail.startswith("local copy only")
                (ok if fine else warn)(line)
            elif not why:
                tool, summary = LAB_CHECKS[system]
                reading = await jarvis.tools.dispatch("doctor", tool, {}, audit=False)
                if "error" in reading:
                    (warn if release_check else fault)(f"{system}: does not answer: {reading['error']}")
                else:
                    ok(f"{system}: {summary(reading)}")
        # The browser on the owner's desktop: no vault needed. A desktop that is off is not a release's fault.
        if jarvis.browser:
            warn(f"browser: no tools ({jarvis.browser})")
        elif jarvis.browser == "":
            tabs = await jarvis.tools.dispatch("doctor", "browser_tabs", {}, audit=False)
            if "error" in tabs:
                warn(f"browser: does not answer: {tabs['error']}")
            else:
                count = len(tabs["tabs"])
                ok(f"browser: the door to the desktop's browser answers, {count} tab{'' if count == 1 else 's'} open")
        if jarvis.backend is None:
            ok(f"no local model is set (JARVIS_LOCAL_MODEL=none in {settings.site}); Jarvis cannot answer until one is")
            return 1 if faults else 0
        try:
            resp = await jarvis.client.get(f"{settings.ollama_url}/api/tags", timeout=10)
            resp.raise_for_status()
            names = [model.get("name", "") for model in resp.json().get("models", [])]
        except (httpx.HTTPError, ValueError) as exc:
            fault(f"Ollama does not answer at {settings.ollama_url} ({type(exc).__name__}). See: journalctl -u ollama")
            return 1
        if _named(settings.model) not in names:
            fault(f"Ollama does not hold the model {settings.model}. Run: update --repair")
            return 1
        if quick:
            ok(f"Ollama answers and holds {settings.model} (not asked anything: --quick)")
            return 1 if faults else 0
        started = time.monotonic()
        said: list[str] = []
        last: dict[str, Any] = {}
        async with contextlib.aclosing(jarvis.router.turn("doctor", DOCTOR_QUESTION, user="doctor")) as events:
            async for event in events:
                if event["type"] == "token":
                    said.append(event["text"])
                last = event
        took = round(time.monotonic() - started, 1)
        if last.get("type") != "done":
            fault(f"{settings.model} did not answer: {last.get('message', 'no answer')}")
        elif not "".join(said).strip():
            fault(f"{settings.model} answered with nothing")
        else:
            speed = last.get("tokens_per_second")
            where = "not recorded" if scratch else f"audit record {last['audit_seq']}"
            ok(f"{settings.model} answered in {took} seconds{f' ({speed} tokens a second)' if speed else ''}; {where}")
        return 1 if faults else 0
    finally:
        if client is None:
            await jarvis.close()
        if scratch is not None:
            scratch.cleanup()


def audit(settings: Settings, out: TextIO) -> int:
    path = settings.audit_path
    if not path.exists():
        print(f"{path}: no records yet", file=out)
        return 0
    good, count, message = verify(path)
    print(f"{path}: {count} records, {message}", file=out)
    return 0 if good else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis", description="Jarvis, the assistant of this lab.",
                                     epilog="With no command, jarvis starts a conversation in this terminal.")
    parser.add_argument("--version", action="store_true", help="print the installed release and leave")
    commands = parser.add_subparsers(dest="command")
    talk = commands.add_parser("chat", help="talk to Jarvis; with words after it, ask one question")
    talk.add_argument("words", nargs="*", help="the question; none starts a conversation")
    talk.add_argument("--spoken", action="store_true", help="answer the way a spoken question is answered: short")
    check = commands.add_parser("doctor", help="check that Jarvis is in working order")
    check.add_argument("--quick", action="store_true", help="do not ask the model anything")
    check.add_argument("--release-check", action="store_true",
                       help="as the installer runs it: a damaged audit log is a warning, not a fault of the release")
    commands.add_parser("audit", help="check that nothing in the audit log was changed or removed")
    commands.add_parser("serve", help="run the service the panel talks to (started by systemd, not by hand)")
    args = parser.parse_args(argv)
    # The jarvis command starts this program in the background of a script, where Ctrl-C is switched off and
    # Python would leave it so. It is passed on by that script and means here what it always means.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        settings = Settings.load()
    except SettingsError as exc:
        print(f"jarvis: {exc}", file=sys.stderr)
        return 1
    if args.version:
        print(settings.release or "not installed from a release")
        return 0
    try:
        if args.command == "doctor":
            return asyncio.run(doctor(settings, args.quick, sys.stdout, release_check=args.release_check))
        if args.command == "audit":
            return audit(settings, sys.stdout)
        if args.command == "serve":
            from .server import serve  # the web server's packages are loaded only where they are used

            return serve(settings)
        return chat(settings, getattr(args, "words", []), getattr(args, "spoken", False))
    except PermissionError as exc:
        print(f"jarvis: {exc.filename or settings.audit_path} cannot be used by the user {getpass.getuser()}. "
              "Run jarvis as root or as the user jarvis; if it still fails, as root: update --repair", file=sys.stderr)
        return 1
    except AuditDamaged as exc:
        print(f"jarvis: {exc}. Jarvis will not add to a log it cannot continue: look at it, then move it away to "
              "start a new one.", file=sys.stderr)
        return 1
