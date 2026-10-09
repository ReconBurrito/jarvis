#!/usr/bin/env python3
"""Keeps secrets and site details out of this public repository.

    scripts/leak_gate.py                 what is staged for the next commit
    scripts/leak_gate.py --all           every tracked or untracked-but-not-ignored file as it is on disk
    scripts/leak_gate.py --history       everything every branch and tag holds: files, their names, commit
                                         messages, authors, tag messages, branch and tag names
    scripts/leak_gate.py --push [REMOTE] the same, for what a push is about to send (the pre-push hook
                                         passes git's list of pushed refs on standard input); commits
                                         the named remote is already known to hold are not read again
    scripts/leak_gate.py --message FILE  one commit message

The hooks in scripts/hooks run it before a commit, on a merge, on the commit message and before a push.
Turn them on once per clone:  git config core.hooksPath scripts/hooks

Not everything passes a hook at the time: a fast-forward merge, a cherry-pick, a rebase and --no-verify do
not. The push check catches those. Nothing here runs for edits made on the hosting site itself.

It refuses, by name: .env files, answers files other than the two examples in defaults/, key and
certificate files.
It refuses, by content: private network addresses, hardware addresses, keys, certificates, key fingerprints
and access tokens. Addresses and hardware addresses from the ranges reserved for documentation pass
(192.0.2.x, 198.51.100.x, 203.0.113.x, 00:00:5E:00:53:xx), so examples use those.

A line that must carry a flagged address for a good reason can carry the words leak-ok; that shows in
the diff for whoever reviews it. The marker excuses addresses only, never keys or tokens.

A site can add its own patterns (its domain, host names, key strings), one regular expression per line,
in a file outside the repository: --denylist FILE, or $JARVIS_LEAK_DENYLIST, or
~/.config/jarvis/leak-denylist. Those are matched without regard to case, against names and content,
and leak-ok does not excuse them. A push is refused when no such file is found, because a check that
silently knows nothing about the site looks the same as a clean one; set JARVIS_LEAK_DENYLIST=none to say
that there is no site to protect.

It is a net, not a proof: it knows shapes, not meanings. A password in a sentence passes it. Pictures are
judged by name only. Only the standard library is used. Exit status 1 when anything is found, 2 when the
check itself could not run.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
from pathlib import PurePosixPath

MARKER = "leak-ok"

# Matched against the file name in lower case.
REFUSED_NAMES = [
    ".env", ".env.*", ".env-*", ".env~", ".envrc", "*.env", "*.env.*", "*.env~", "*.vars", "*.vars.*", "*.vars~",
    ".netrc", "*.age", "age*.txt", "wg*.conf",
    "*.pem", "*.key", "*.crt", "*.cer", "*.der", "*.p12", "*.pfx", "*.jks", "*.ppk", "*.kdbx", "*.b64",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*", "keys.txt", "key.txt", "*-key.txt", "*_key.txt",
]
ALLOWED_NAMES = ["*.env.example"]
ALLOWED_PATHS = {"defaults/example.vars", "defaults/example-desktop.vars"}

# (first address, last address) of the ranges that describe a private network.
PRIVATE_RANGES = [
    ((10, 0, 0, 0), (10, 255, 255, 255)),
    ((172, 16, 0, 0), (172, 31, 255, 255)),
    ((192, 168, 0, 0), (192, 168, 255, 255)),
    ((100, 64, 0, 0), (100, 127, 255, 255)),
    ((169, 254, 0, 0), (169, 254, 255, 255)),
]
# Looked for at every position, so an address glued to other dotted numbers is still seen.
ADDRESS = re.compile(r"(?<![0-9])(?=((?:[0-9]{1,3}\.){3}[0-9]{1,3})(?![0-9]))")
REVERSE = re.compile(r"(?<![0-9])((?:[0-9]{1,3}\.){3}[0-9]{1,3})\.in-addr\.arpa", re.IGNORECASE)
PRIVATE_V6 = re.compile(r"(?<![0-9A-Fa-f:])f[cd][0-9a-f]{2}:[0-9a-f:]{2,}", re.IGNORECASE)
HARDWARE = re.compile(r"(?<![0-9A-Fa-f])(?=([0-9A-Fa-f]{2}([:-])(?:[0-9A-Fa-f]{2}\2){4}[0-9A-Fa-f]{2})(?![0-9A-Fa-f]))")
# Ways of writing a private address that are not four numbers with dots: the last part left open
# (a.b.c.x), and dashes as in host names made from an address.
ADDRESS_OPEN = re.compile(r"(?<![0-9.])(?:192\.168|10\.[0-9]{1,3}|172\.(?:1[6-9]|2[0-9]|3[01])|100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7]))"
                          r"\.[0-9]{1,3}\.[xX*](?![A-Za-z0-9])")
ADDRESS_DASHED = re.compile(r"(?<![0-9])(?:192-168-[0-9]{1,3}-[0-9]{1,3}|100-(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])-[0-9]{1,3}-[0-9]{1,3}"
                            r"|ip-10(?:-[0-9]{1,3}){3})(?![0-9])")
HARDWARE_DOTTED = re.compile(r"(?<![0-9A-Fa-f.])(?=[0-9.]*[A-Fa-f])[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}(?![0-9A-Fa-f.])")
DOC_HARDWARE = re.compile(r"^00[:-]00[:-]5e[:-]00[:-]53[:-]", re.IGNORECASE)

# Each pattern is written so that this file does not match itself.
PATTERNS = [
    ("key or certificate block", re.compile(r"-----BEGIN [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE)", re.IGNORECASE)),
    ("key or certificate block, encoded", re.compile(r"LS0tLS1CRUdJTi[A-Za-z0-9+/]{8,}")),
    ("PuTTY key", re.compile(r"PuTTY-User-Key-File-[0-9]", re.IGNORECASE)),
    ("age secret key", re.compile(r"AGE-SECRET-KEY-(?:PQ-)?1[A-Z0-9]{20,}", re.IGNORECASE)),
    ("age plugin identity", re.compile(r"AGE-PLUGIN-[A-Z0-9]+-1[A-Z0-9]{10,}", re.IGNORECASE)),
    ("age recipient", re.compile(r"\bage1[a-z0-9]{58}\b", re.IGNORECASE)),
    ("age encrypted file", re.compile(r"-----BEGIN AGE ENCRYPTE[D] FILE|\bage-encryption\.org/v[0-9]", re.IGNORECASE)),
    ("sops encrypted value", re.compile(r"ENC\[AES256_GCM,data:")),
    ("certificate pin", re.compile(r"\bsha256//[A-Za-z0-9+/]{43}=")),
    ("assigned secret", re.compile(r"^\s*(?:export\s+)?[A-Z][A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|KEY)[A-Z0-9_]*=[\"']?(?![/$<{])[A-Za-z0-9+/=_-]{20,}[\"']?\s*$")),
    ("key fingerprint", re.compile(r"SHA256:[A-Za-z0-9+/]{43}")),
    ("key fingerprint", re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}:){15,}[0-9A-Fa-f]{2}")),
    ("WireGuard key", re.compile(r"\b(?:private|preshared)key\s*=\s*[A-Za-z0-9+/]{43}=", re.IGNORECASE)),
    ("Proxmox API token", re.compile(r"\bPVEAPIToken=[^\s!]+![^\s=]+=[0-9a-f-]{36}", re.IGNORECASE)),
    ("Proxmox Backup API token", re.compile(r"\bPBSAPIToken=[^\s:]+:[0-9a-f-]{36}", re.IGNORECASE)),
    ("signed web token", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("Anthropic token", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("Slack token", re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}")),
    ("link to a private conversation", re.compile(r"\bclaude\.ai/(?:code/)?(?:session_|chat/|share/)[A-Za-z0-9_-]{6,}", re.IGNORECASE)),
]
# A commit or tag carries the time zone of the machine it was made on, which says where its author lives.
# Whatever this repository publishes is dated in UTC.
LOCAL_TIME = re.compile(rb"^(author|committer|tagger) .* [0-9]+ ([+-][0-9]{4})$", re.MULTILINE)


class GateError(Exception):
    pass


class Refused(str):
    """In place of content: the reason why something is refused without being read."""


def git(*args: str) -> bytes:
    try:
        return subprocess.run(["git", *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    except FileNotFoundError:
        raise GateError("git is not installed")
    except subprocess.CalledProcessError as error:
        reason = error.stderr.decode("utf-8", "replace").strip().splitlines()
        raise GateError(f"git {' '.join(args[:2])} failed: {reason[-1] if reason else error.returncode}")


def name_refused(path: str) -> bool:
    if path in ALLOWED_PATHS:
        return False
    name = PurePosixPath(path).name.lower()
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in ALLOWED_NAMES):
        return False
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in REFUSED_NAMES)


def private_address(text: str) -> bool:
    octets = tuple(int(part) for part in text.split("."))
    if any(octet > 255 for octet in octets):
        return False
    for first, last in PRIVATE_RANGES:
        if first <= octets <= last:
            return octets != first  # 10.0.0.0 and the like name the range, not a host
    return False


def short(text: str) -> str:
    return text if len(text) <= 16 else text[:12] + "..."


def scan_text(text: str, deny: list[re.Pattern[str]]):
    """Yields (line number, what was found) for one file's content."""
    for number, line in enumerate(text.splitlines(), 1):
        for pattern in deny:
            found = pattern.search(line)
            if found:
                yield number, f"site pattern /{pattern.pattern}/: {short(found.group(0))}"
        for what, pattern in PATTERNS:
            found = pattern.search(line)
            if found:
                yield number, f"{what}: {short(found.group(0))}"
        if MARKER in line:
            continue
        seen = set()
        for found in ADDRESS.finditer(line):
            if line[found.end(1):found.end(1) + 13].lower() == ".in-addr.arpa":
                continue  # a reversed address; judged below, the right way round
            if private_address(found.group(1)) and found.group(1) not in seen:
                seen.add(found.group(1))
                yield number, f"private address: {found.group(1)}"
        for found in REVERSE.finditer(line):
            forward = ".".join(reversed(found.group(1).split(".")))
            if private_address(forward):
                yield number, f"private address: {found.group(0)}"
        for pattern in (PRIVATE_V6, ADDRESS_OPEN, ADDRESS_DASHED):
            for found in pattern.finditer(line):
                yield number, f"private address: {found.group(0)}"
        seen = set()
        for found in HARDWARE.finditer(line):
            if not DOC_HARDWARE.match(found.group(1)) and found.group(1) not in seen:
                seen.add(found.group(1))
                yield number, f"hardware address: {found.group(1)}"
        for found in HARDWARE_DOTTED.finditer(line):
            yield number, f"hardware address: {found.group(0)}"


def as_text(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    if b"\0" in data:
        # Text saved two bytes a letter without a mark, or a program file: read what letters are in it.
        return data.replace(b"\0", b"").decode("latin-1")
    return data.decode("utf-8", errors="replace")


def scan_file(path: str, data: bytes | None, deny: list[re.Pattern[str]]):
    if name_refused(path):
        yield 0, "a file of this name does not belong in the repository"
    for _, what in scan_text(path, deny):
        yield 0, f"in the file name: {what}"
    if data is None:
        yield 0, "a repository inside the repository (submodule); its content cannot be checked"
        return
    yield from scan_text(as_text(data), deny)


def shown(raw: bytes) -> str:
    return raw.decode("utf-8", errors="backslashreplace")


def staged():
    fields = git("diff", "--cached", "--raw", "--no-abbrev", "--no-renames", "-z", "--diff-filter=ACMT").split(b"\0")
    for meta, path in zip(fields[0::2], fields[1::2]):
        _, mode, _, blob, _ = meta.decode().lstrip(":").split()
        name = shown(path)
        yield name, name, None if mode == "160000" else git("cat-file", "blob", blob)


def on_disk():
    for path in git("ls-files", "-co", "--exclude-standard", "-z").split(b"\0"):
        if not path:
            continue
        name = shown(path)
        if os.path.isdir(path) and not os.path.islink(path):
            yield name, name, None
            continue
        try:
            with open(path, "rb") as handle:
                yield name, name, handle.read()
        except (FileNotFoundError, IsADirectoryError):
            continue


def history(tips: list[str] | None = None, refs: list[str] | None = None, known: list[str] | None = None):
    """Everything reachable from `tips` (default: every ref) and not from `known`, and the names of `refs`
    (default: every ref)."""
    if refs is None:
        refs = [line for line in git("for-each-ref", "--format=%(refname)").decode("utf-8", "replace").splitlines()
                if not line.startswith("refs/stash")]
    if tips is None:
        tips = refs
    for ref in refs:
        yield f"{ref}: its name", "", ref.encode()
    seen_tags = set()
    for tip in tips:
        # An annotated tag is an object of its own, with a message and the name and address of who made it.
        tag = tip
        while git("cat-file", "-t", tag).strip() == b"tag" and tag not in seen_tags:
            seen_tags.add(tag)
            body = git("cat-file", "tag", tag)
            yield f"{tip}: tag message", "", body
            yield from local_time(f"{tip}: tag", body)
            tag = body.split(b"\n", 1)[0].split(b" ", 1)[1].decode()
        if git("cat-file", "-t", tag).strip() != b"commit":
            yield f"{tip}: what it points at", "", Refused("not a commit; its content cannot be checked")
    seen = set()
    commits = [tip + "^{commit}" for tip in tips if git("cat-file", "-t", tip + "^{}").strip() == b"commit"]
    for commit in git("rev-list", *commits, "--not", *(known or []), "--").decode().split() if commits else []:
        yield f"{commit[:12]}: author and committer", "", git("log", "-1", "--format=%an <%ae>%n%cn <%ce>", commit)
        yield from local_time(f"{commit[:12]}: dates", git("cat-file", "commit", commit).split(b"\n\n", 1)[0])
        yield f"{commit[:12]}: commit message", "", git("log", "-1", "--format=%B", commit)
        for entry in git("ls-tree", "-r", "-z", commit).split(b"\0"):
            if not entry:
                continue
            meta, path = entry.split(b"\t", 1)
            _, kind, blob = meta.decode().split()
            if (blob, path) in seen:
                continue
            seen.add((blob, path))
            name = shown(path)
            yield f"{commit[:12]}:{name}", name, git("cat-file", "blob", blob) if kind == "blob" else None


def local_time(where: str, head: bytes):
    """A finding for each date in a commit's or tag's header that is not in UTC."""
    for who, offset in LOCAL_TIME.findall(head):
        if offset not in (b"+0000", b"-0000"):
            yield f"{where} {who.decode()} date", "", Refused(
                f"dated in the time zone {offset.decode()}, which says where its author is; make it in UTC "
                "(TZ=UTC, or GIT_COMMITTER_DATE and GIT_AUTHOR_DATE ending in +0000)")


def pushed(remote: str):
    """What `git push` is about to send, from the lines git gives a pre-push hook: local ref, local id, remote
    ref, remote id. A deleted ref sends nothing. Commits the remote holds already are not read again: those
    its own current id of the ref names, and those under this clone's record of that remote's branches."""
    tips, refs, known = [], [], []
    for line in sys.stdin.buffer.read().decode("utf-8", "backslashreplace").splitlines():
        parts = line.rsplit(" ", 3)
        if len(parts) != 4 or not all(re.fullmatch(r"[0-9a-f]{40,64}", part) for part in (parts[1], parts[3])):
            raise GateError(f"a line from git about what is pushed could not be read: {line!r}")
        if set(parts[1]) == {"0"}:
            continue
        tips.append(parts[1])
        refs.append(parts[2])
        if set(parts[3]) != {"0"} and subprocess.run(["git", "cat-file", "-e", parts[3] + "^{commit}"],
                                                      stderr=subprocess.DEVNULL).returncode == 0:
            known.append(parts[3])
    if remote and re.fullmatch(r"[A-Za-z0-9._-]+", remote):
        known += git("for-each-ref", "--format=%(objectname)", f"refs/remotes/{remote}/").decode().split()
    return history(tips, refs, known)


def load_denylist(explicit: str | None, required: bool) -> list[re.Pattern[str]]:
    named = explicit or os.environ.get("JARVIS_LEAK_DENYLIST")
    if named == "none":
        return []
    path = named or os.path.expanduser("~/.config/jarvis/leak-denylist")
    if not os.path.isfile(path):
        if named or required:
            raise GateError(f"there is no file of site patterns at {path}. Write one (see the top of scripts/leak_gate.py), "
                            "or set JARVIS_LEAK_DENYLIST=none if there is no site to protect")
        return []
    patterns = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                patterns.append(re.compile(line, re.IGNORECASE))
            except re.error as error:
                raise GateError(f"{path} line {number} is not a regular expression: {error}")
    return patterns


def run(args) -> int:
    explicit = args.denylist if args.denylist in (None, "none") else os.path.abspath(args.denylist)
    deny = load_denylist(explicit, required=args.push is not None)
    if args.message:
        try:
            with open(args.message, "rb") as handle:
                # With `git commit -v` the change itself follows a scissors line; it is not part of the message.
                body = re.split(rb"(?m)^# -+ >8 -+\r?$", handle.read(), maxsplit=1)[0]
        except OSError as error:
            raise GateError(f"the message file cannot be read: {error}")
        source, scope = [("commit message", "", body)], "commit message"
    else:
        os.chdir(shown(git("rev-parse", "--show-toplevel")).rstrip("\n"))
        if args.push is not None:
            source, scope = pushed(args.push), "names, messages and files about to be pushed"
        elif args.history:
            source, scope = history(), "names, messages and files of the history"
        elif args.all:
            source, scope = on_disk(), "files on disk"
        else:
            source, scope = staged(), "staged files"
    count = findings = 0
    for label, path, data in source:
        count += 1
        if isinstance(data, Refused):
            found = [(0, str(data))]
        elif path:
            found = scan_file(path, data, deny)
        else:
            found = scan_text(as_text(data), deny)
        for number, what in found:
            findings += 1
            print(f"{label}:{number}: {what}" if number else f"{label}: {what}")
    sites = f"{len(deny)} site patterns" if deny else "no site patterns"
    if findings:
        print(f"leak gate: {findings} finding(s) in {count} {scope}, {sites}. Nothing may be committed or pushed until they are gone.",
              file=sys.stderr)
        return 1
    print(f"leak gate: clean ({count} {scope}, {sites}).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Keep secrets and site details out of the repository.")
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--all", action="store_true", help="every file on disk that git tracks or would add")
    where.add_argument("--history", action="store_true", help="everything every branch and tag holds")
    where.add_argument("--push", nargs="?", const="", metavar="REMOTE", help="what a push is about to send (pre-push hook)")
    where.add_argument("--message", metavar="FILE", help="one commit message")
    parser.add_argument("--denylist", help="a file of site patterns, one regular expression per line")
    try:
        return run(parser.parse_args())
    except GateError as error:
        print(f"leak gate: could not run: {error}. Treat this as a refusal.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
