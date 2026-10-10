# Jarvis

A self-hosted assistant for a home lab that you talk to and that looks after the lab with you: it reads the
state of your hypervisor, firewall, switch and backups, answers from what it actually sees, and changes
something only after you approve the exact change. You use it from one place, a Linux desktop streamed to
your browser, with Jarvis docked beside its own web browser.

It runs in two unprivileged containers on Proxmox VE:

| Container | What it holds |
|---|---|
| brain | Jarvis itself, the local language model on your GPU, the voice, and the credentials for your lab |
| desktop | The streamed desktop and the browser Jarvis drives. No credentials, because that browser opens pages from the internet |

## Status

Under construction. So far: the installers, the update command, the desktop (the streamed Linux desktop
with Jarvis's browser), the local model on the brain's GPU, Jarvis's core, and Jarvis's panel on the
desktop, where you talk to it, its vault, and read-only tools for Proxmox, Proxmox Backup Server, OPNsense and
the DNS servers and the switch, its own notes, which it searches by words and by meaning and writes to, and
the browser on the desktop, where it opens and reads web pages, and its own machine's files, which it reads and
changes. The
voice arrives in the releases that follow. A full guide with pictures comes with the first complete release.


## Rules for this repository

This repository is public, and the lab it was first built for is not. Everything in it is written so that it
tells nothing about any one site:

1. Test data is invented. Output captured from a real device, a real log or a real API answer is never pasted
   into a test, not even with the names changed: the shape gives the rest away (which ports carry which VLANs,
   which versions run where, which guest is which).
2. Examples use the addresses and hardware addresses reserved for documentation (192.0.2.x, 198.51.100.x,
   203.0.113.x, 00:00:5E:00:53:xx), and numbers that mean nothing at any site.
3. Commits and tags are dated in UTC, and carry no links to private conversations.
4. `scripts/leak_gate.py` checks every commit, merge and push for secrets, private addresses, hardware
   addresses, local time zones and private links, and a site's own list of names kept outside the repository.
   Turn its hooks on in every clone: `git config core.hooksPath scripts/hooks`.

## Install

Run as root in the shell of a Proxmox VE node (8.2 or newer). The installer asks a few questions (Default) or all of them
(Advanced), shows a summary, and creates the container only after you confirm.

```
bash -c "$(curl -fsSL https://raw.githubusercontent.com/ReconBurrito/jarvis/main/ct/jarvis.sh)"
```

```
bash -c "$(curl -fsSL https://raw.githubusercontent.com/ReconBurrito/jarvis/main/ct/jarvis-desktop.sh)"
```

The brain installer always asks where Jarvis keeps its `.env` files and how: plain files that only root and
the `jarvis` user can read, or files encrypted with sops and age (see The vault, below).

The desktop installer always asks who may open the desktop and at which web address (see The desktop,
below).

The two containers find each other through two settings (see Jarvis's panel, below): the brain is told the
desktop's address (`var_panel_allow`), and the desktop is told the brain's container ID (`var_brain`).
Either can be given at the first install or by running the installer again.

### Without questions

Put the answers in a file and name it. No question is asked; a missing or wrong answer stops the installer
before anything is changed. `defaults/example.vars` lists every setting, and
`defaults/example-desktop.vars` is the same for the desktop. The file must name the container ID
(`var_ctid`), so that a second run finds the container of the first.

```
JARVIS_ANSWERS=/root/jarvis.vars bash -c "$(curl -fsSL https://raw.githubusercontent.com/ReconBurrito/jarvis/main/ct/jarvis.sh)"
```

The same names work as environment variables (`var_cpu=8`) and win over the file.

| Setting | Meaning | Default |
|---|---|---|
| `var_ctid` | Container ID | next free ID (must be given in an answers file) |
| `var_hostname` | Hostname | `jarvis`, `jarvis-desktop` |
| `var_container_storage`, `var_template_storage` | Where the disk and the template go | `local-lvm`, `local` |
| `var_disk`, `var_cpu`, `var_ram`, `var_swap` | GB, cores, MB, MB | 48, 6, 6144, 0 (brain); 32, 4, 3072, 512 (desktop) |
| `var_brg`, `var_vlan`, `var_mac` | Bridge, VLAN tag, MAC address | `vmbr0`, none, automatic |
| `var_net`, `var_gateway` | `dhcp`, or an address such as `192.0.2.10/24` with its gateway | `dhcp` |
| `var_expect_ip` | With DHCP: stop if any other address is handed out | not checked |
| `var_gpu`, `var_gpu_gid` | Render device to share, and its group in the container | first `/dev/dri/renderD*`, 993 |
| `var_timezone`, `var_tags`, `var_onboot` | Time zone (`host`, `UTC` or a zone name), Proxmox tags, start at boot | `host`, `jarvis`, `yes` |
| `var_env_dir`, `var_secrets` | Folder of the `.env` files (new or empty, under `/etc/jarvis/`, `/srv/`, `/opt/`, `/mnt/` or `/media/`), and `plain` or `sops` (brain only; see The vault) | `/etc/jarvis/secrets`, `plain` |
| `var_model`, `var_embed_model` | The local model Jarvis thinks with and the one for searching notes, as Ollama names them, or `none` (brain only) | `qwen3:8b`, `qwen3-embedding:0.6b` |
| `var_panel_allow` | Addresses that may reach Jarvis's panel port: the desktop container (brain only) | nobody |
| `var_brain` | Container ID of the brain on this node whose panel the desktop shows (desktop only) | none |
| `var_desktop_allow`, `var_desktop_origin` | Addresses that may open the desktop (your reverse proxy), and the web address it is opened at (desktop only) | nobody, none |
| `var_release`, `var_branch` | `signed` releases, or a `branch` followed unchecked | `signed`, `main` |
| `var_repo` | Repository the container installs from | `ReconBurrito/jarvis` |
| `var_allow_low_memory` | Install although the node is short of memory | `no` |

Running an installer again for a container it made carries on where it stopped. The container itself (size,
network, devices) is left as it is, and of the other settings only those you name again are applied. A
container it did not make is never touched.

## The local model

The brain runs its model with [Ollama](https://ollama.com), which answers on that container only
(127.0.0.1). When the container was given a render device (`var_gpu`), Ollama reaches the GPU through
Vulkan; that is how GPUs without CUDA (Intel, AMD) work inside a Proxmox container.

1. Ollama is taken from its own release on GitHub, by version, and the archive must match the checksum
   recorded in `brain/ollama`. A release of Jarvis decides which Ollama runs.
2. The models named by `var_model` and `var_embed_model` are fetched once (several GB; the container needs
   a way to the internet for it). Any model from Ollama's library can be named; `none` goes without.
3. After every install, update and `--check` the model is loaded and Ollama is asked where it sits. On the
   GPU in full: good. Partly: a warning, answers will be slow. On the processor although there is a GPU:
   a fault, and the install says so. Without a GPU the model runs on the processor and that is said.

To change the model later, run the installer again with `var_model=NAME` before the command.

## The vault

Jarvis's credentials for the lab live in `.env` files in the folder chosen at install
(`/etc/jarvis/secrets` unless you named another). With `var_secrets=sops`, which this section is about, they are
encrypted with [sops](https://github.com/getsops/sops) for an age identity of the brain, and the brain keeps that
identity sealed with a passphrase:

1. Once, as root in the brain (`pct enter`, then):

   ```
   jarvis-unlock --setup
   ```

   It makes the identity, asks for a passphrase of at least 8 characters (twice; an empty one is refused, never
   made up for you), proves the sealed file opens with it, and prints the identity's public key. Add that key as a recipient of your vault file in your
   `.sops.yaml`, run `sops updatekeys` on the file, and copy the file into the folder. The installer makes every
   file there readable for Jarvis and nobody else.
2. After every restart of the brain, as root in the brain:

   ```
   jarvis-unlock
   ```

   Until then Jarvis runs, says in the panel that its vault is locked, and has no tools for the lab. It notices
   the unlock within seconds; nothing restarts. `jarvis-unlock --lock` locks it again, and `--status` says how it
   stands and whether each file is encrypted for this brain.

The identity rests on the disk only sealed (age's own passphrase encryption), in a folder only root can read.
Unlocked, it is in `/run`, which is memory-backed and emptied by a restart. Decrypted values exist only in
Jarvis's memory, never on the disk; they are never shown to the model, and they are masked wherever they would
appear in a tool's answer or the audit log. sops is taken from its own release by version and checksum
(`brain/sops`), like Ollama.

What Jarvis reads with it so far:

| System | Vault values | Also needed |
|---|---|---|
| Proxmox VE, read-only | `JARVIS_PVE_TOKEN_ID` (such as `jarvis@pve!ro`), `JARVIS_PVE_TOKEN_SECRET`, `JARVIS_PVE_HOSTS` (addresses with commas between; `address=name` checks the node's certificate for that name) | The cluster's certificate authority, which the installer on the node hands to the brain on every run |
| Proxmox Backup Server, read-only | `JARVIS_PBS_TOKEN_ID` (such as `jarvis@pbs!ro`), `JARVIS_PBS_TOKEN_SECRET`, `JARVIS_PBS_HOSTS` (as above; name the certificate's name, since a self-signed one seldom holds the address) | The server's own certificate as `/etc/jarvis/trust/pbs.pem` (owner root, mode 0644), pinned: check its fingerprint against the one on the server's dashboard |
| OPNsense, read-only | `JARVIS_OPNSENSE_API_KEY`, `JARVIS_OPNSENSE_API_SECRET`, `JARVIS_OPNSENSE_HOSTS` (as above) | The web certificate as `/etc/jarvis/trust/opnsense.pem`, pinned; pin it again when OPNsense renews it |
| A TP-Link (Omada) switch, read-only, over SSH | `JARVIS_SWITCH_USER`, `JARVIS_SWITCH_PASSWORD` (a view-only user), `JARVIS_SWITCH_HOST` (its address) | Its SSH host key as `/etc/jarvis/trust/switch_known_hosts`, one line as `ssh-keyscan` prints it, checked against the key you know; the password is sent only after that key is verified |
| Jarvis's notes: a git repository of Markdown files (sources, wiki pages, daily notes) | `JARVIS_NOTES_REPO` (such as `git@github.com:you/notes.git`), `JARVIS_NOTES_DEPLOY_KEY` (a deploy key with write access to that one repository: the private key, or it in base64) | GitHub's published host keys, which the installer puts in `/etc/jarvis/trust/github_known_hosts`. The key is held by an ssh-agent of Jarvis's own, in memory only. |
| DNS servers (such as PiHoles) | `JARVIS_DNS_SERVERS` (`address=name` with commas between), `JARVIS_DNS_LAB_NAME` (a name only your own DNS answers) | Nothing: no credential is used; each server is asked three ordinary questions |

A system with none of its values in the vault is simply left out. `jarvis doctor` reads each system that is set
up and says what it answered. The `_HOST`, `_HOSTS`, `_SERVERS` and `_LAB_NAME` values only say where things are, so they
are not masked in Jarvis's answers; everything else is.

Make every credential read-only. Proxmox: an API token of its own user with the `PVEAuditor` role on `/`, privilege
separation on. Backup server: a token with the `Audit` role on `/`. OPNsense: a user holding only the "Lobby:
Dashboard" privilege, with an API key. Jarvis only ever sends GET requests, to a fixed list of paths.

## Jarvis's notes

Jarvis keeps a clone of its notes repository in `/var/lib/jarvis/notes` and brings it up to date when the vault
opens and before every change. It answers from its notes with `notes_search` (by words, and by meaning through the
embedding model) and `notes_read`, naming the note it used. When you ask it to write something down it uses
`note_add` or `note_replace`, and the rules are kept in code, whatever the model asks for:

1. An ordinary note is changed on main, one commit per change, in Jarvis's name, and sent to the remote.
2. `SOUL.md`, `MEMORY.md`, `USER.md`, `HEARTBEAT.md`, `SCHEMA.md` and everything under `skills/` shape how Jarvis
   behaves, so a change to them goes to a branch `proposal/note-NAME` and takes effect when you merge it.
3. Files under `raw/` are sources, kept as they were filed, and never changed. Nothing is written through a link.
4. Text that looks like a secret, or holds a value of the vault, is never written.
5. The audit log records which note changed and the commit, never the text.

Where the notes are kept, one of three ways:

1. **On the brain only** (the default). With no notes repository in the vault, Jarvis keeps its notes in a git
   repository in `/var/lib/jarvis/notes/repo` and begins it with a first note when there is none. Nothing leaves
   the brain; the notes are as safe as the brain's backups. The installer sets `JARVIS_NOTES_LOCAL=on` in
   `/etc/jarvis/site.env`; set it to `off` for no notes at all.
2. **In a private repository of your own on GitHub** (or any git server over SSH). Make an empty private
   repository, make a deploy key for it with write access, and put two lines in the vault:
   `JARVIS_NOTES_REPO=git@github.com:YOU/REPO.git` and `JARVIS_NOTES_DEPLOY_KEY=` followed by the private key file
   in base64 (`base64 -w0 keyfile`, or `[Convert]::ToBase64String([IO.File]::ReadAllBytes("keyfile"))` in
   PowerShell). GitHub's host keys are pinned already. An empty repository is given the first note; notes Jarvis
   kept on the brain until then move there, history and all, as long as the repository is still empty. Never use
   a public repository: the notes describe your lab and you.
3. **In a git repository in a folder on the brain** that you manage yourself, such as a bare repository at
   `/var/lib/jarvis/notes/origin.git`, made as the user jarvis (Jarvis's service may write only under
   `/var/lib/jarvis/notes`, and git does not use a repository another user owns):
   `runuser -u jarvis -- git init --bare -b main /var/lib/jarvis/notes/origin.git`, then
   `JARVIS_NOTES_REPO=/var/lib/jarvis/notes/origin.git` in the vault, no key needed.

`jarvis doctor` says which of these is in use and whether it works.

The **Notes** link at the foot of the panel opens the notes in a window of their own: a shelf of notes (the standing
files first, then each folder, with a filter), the note as it reads, and Edit to change its text. A link from one
note to another opens it there. Saving (the button, or Ctrl+S) commits on main in the name "Owner", with Jarvis as
committer, and you may change the standing files directly. A save names the version it started from: when the note
changed meanwhile, nothing is overwritten, and what you typed stays in the editor. Sources under `raw/` open read
only. The window renders Markdown with its own small renderer, which builds the page from text only, so nothing in a
note can become markup or script.

## Jarvis's panel

The panel docked at the edge of the desktop is where you talk to Jarvis: a box to type in, the
conversation, a line that says what Jarvis is doing, what waits for your yes or no, and a few readings of
the lab (only the brain's own machine so far). Stop ends an answer at once; New conversation starts over.

The panel is served by the brain, on one HTTPS port (8443), and the desktop's start page goes there as
soon as the brain answers. To link the two:

1. Tell the brain the desktop's address: `var_panel_allow=192.0.2.201` in its answers file (or before the
   command), and run the brain's installer again.
2. Tell the desktop the brain's container ID: `var_brain=200`, and run the desktop's installer again. It
   reads the brain's address and certificate authority from that container on the node and hands them to
   the desktop. Nothing in the brain is changed by this.

What protects that port:

1. A firewall rule in the brain lets only the named addresses reach it. It is loaded before the network
   comes up, and Jarvis's service does not start without it. Nothing else about the brain's network is
   decided by that rule.
2. The brain makes a certificate authority of its own, whose key only root can read, and a certificate for
   its own address and nothing else. The desktop's browser is told, by policy, to trust that authority for
   that one address only. No certificate is trusted by hand, and no warning is clicked away.
3. The desktop's browser also shows pages from the internet, and such a page could make the browser send
   a request to the brain. The service answers only what the panel's own pages ask for (every browser
   says truthfully which page a request comes from), only under the brain's own address as its name, and
   the panel cannot be shown inside another page. The panel loads no script, style or picture from
   anywhere but the brain.
4. The service runs as the user `jarvis` and can write its audit log and its notes, and nothing else on the system.

What this does not cover: a program running as the desktop's user outside the browser can say what it
likes to the brain, and so can anything that drives the desktop's browser. The notes window speaks for you: what it
saves is saved in your name, standing files included. The browser keeps pages in its sandbox so that there is no
such program. Jarvis drives its browser on the desktop only so far as the next section says, and never on the
brain's own pages. Before Jarvis is given tools that change the lab, approvals get a way of their own that the
desktop cannot fake.

## Jarvis's browser

Jarvis has three read-only tools for the browser that fills the desktop beside its panel: which tabs are open
(`browser_tabs`), open a web page in a new tab where you see it (`browser_open`), and read the text of a page
that is open (`browser_read`). It cannot click, type, scroll or sign in, and says so. What a page says is handed
to the model as information, never as an instruction.

That browser is Jarvis's, not yours: it runs as a user of its own (uid 1001) with a profile of its own, and the
desktop's firewall lets that user reach the internet over IPv4 and the name servers, and nothing private: not
the brain, not the lab, not the desktop's own ports, and no IPv6 at all (there a lab's machines have global
addresses like the internet's). You see it and can use it, but the lab's own pages do not open there,
and what you sign in to there, Jarvis can read. It is kept open from outside the desktop
(`desktop/jarvis-browser-keeper.sh`, the service `jarvis-browser`).

How the brain reaches that browser, and who else cannot:

1. The browser opens its DevTools port (9222) on the desktop container's loopback only. DevTools has no login
   of its own and gives whoever reaches it the whole browser.
2. A TLS door (`socat`, port 9223, as a user of its own) leads to it, and only while what listens there is
   Jarvis's browser's user (`desktop/jarvis-door-forward.sh`). It takes TLS 1.3 only, and only a client
   certificate signed by the brain's own authority for client use; the brain's installer makes that certificate,
   and its key never leaves the brain. The desktop's firewall lets only the brain's address reach the door.
3. The door has a certificate of its own, made by the desktop's installer. The installer on the Proxmox node
   hands it, and where the door is, to the brain (`/etc/jarvis/trust/desktop-door.pem` and `.addr`), which pins
   it. This is the one thing the desktop's installer writes into the brain.
4. The desktop's own user and Jarvis's browser's user, and so every page, cannot reach either port.
5. Jarvis opens only addresses on the internet and does not read a tab that shows anything else. While it opens
   or reads a page, every request of that tab and of its frames and workers, redirects included, goes through
   Jarvis first and is stopped unless it is for a public address, and Jarvis says what it stopped. This is the
   second line; the firewall is the first, and it also covers what this one does not see (WebSockets, shared
   workers, now and then a worker started by a frame of another site, and everything the page does after Jarvis
   lets go).

What this does not cover:

1. Anything on the internet that Jarvis's browser is signed in to is open to Jarvis and to what a page tells it,
   so sign in to nothing there that you would not hand Jarvis.
2. A page can try to talk Jarvis into opening an address that carries what Jarvis knows (in its query, say) to a
   site of the page's choosing. Opening a page is not yet something you approve; until it is, treat what Jarvis
   has read in a turn as something a page could ask it to send on.
3. Your network's own public address counts as the internet. If your router sends that address back into your
   network (NAT reflection, port forwards) or shows its own pages there, Jarvis's browser reaches those too.
4. Jarvis's browser shows its windows on the same screen as the panel. Its pages run in Chromium's sandbox; a page
   that broke out of it could see and type into the desktop's other windows. `jarvis doctor` says whether the
door answers.

## Jarvis's own files

Jarvis can read and change the files of its own machine, the brain, so that it can look at and configure
itself: `fs_list`, `fs_read`, `fs_find`, `fs_write`, `fs_mkdir` and `fs_delete`, and `fs_changes` and `fs_undo`
to see and take back what it changed. They work through `jarvis-fsd` (`src/jarvis/selffs/daemon.py`, the
service `jarvis-fs`), which runs as root and answers the user `jarvis` alone, on a socket in `/run/jarvis-fs`.

1. Kept out of reach, for reading and for writing: the brain's private keys, the vault (the `.env` folder and
   the vault identity, sealed and unlocked), the system's password files and SSH keys, `jarvis-fsd`'s own
   history, and `/proc`, `/sys` and `/dev`. A file that holds a private key is not read or changed wherever it
   is, and no key is written.
2. Every change keeps a copy of what it replaced in `/var/lib/jarvis-fs` (root only; the last thousand), so each
   can be undone, newest first. A file larger than 8 MB is not changed, since no copy of it would be kept.
3. A write replaces the whole file at once, keeps its owner and mode, and can name the version it was read as,
   so a change made meanwhile is not lost. Set-user and set-group modes are not given out.
4. Every call is in the audit log; what a write holds is kept there as its length and fingerprint only.
5. Jarvis cannot run commands or restart services: a change to a service's files takes effect when that
   service restarts. Files the installer writes are written again at the next update.

6. Read but never changed: what keeps these guards in place, so that no write can switch them off at the next
   restart or update. That is Jarvis's code and Python environment, `jarvis-fsd` and the units that start it and
   Jarvis, Jarvis's commands, sops, the site settings, the release keys and pinned certificates, and the audit
   log. (Code changes would not last anyway: an update puts the signed release back.) A protected file is known
   by its place and by its inode, so a link, a hard link or a bind mount does not lead around it.

What this does not cover: whatever Jarvis is told to change, it changes. That includes the files that decide
who may reach the brain, so a change can cut off the panel until it is undone (`fs_undo`) or put right by hand
(`update --repair` writes the installer's own files again). It also includes files that make something run as
root, such as other systemd units, cron jobs and root's shell startup files: a change there can, at the next
start, undo any guard above. Treat a request to change such a file as you would giving Jarvis root.

## Talking to Jarvis in a terminal

In the brain container (`pct enter`, then):

```
jarvis
```

That starts a conversation in the terminal; an empty line or Ctrl-D ends it, Ctrl-C stops an answer. It
is a conversation of its own, not the one in the panel. The other forms:

| Command | What it does |
|---|---|
| `jarvis chat "How long has this machine been up?"` | One question, one answer. The answer goes to standard output, notes about tools and speed to standard error |
| `jarvis doctor` | Looks at every part: settings, audit log, tools, and one question to the model through the whole of Jarvis. `--quick` leaves the model alone |
| `jarvis audit` | Checks that no record in the audit log was changed or removed |
| `jarvis --version` | The installed release |

What holds whatever the model says:

1. Jarvis never runs as root. Started by root, the command hands it to the user `jarvis` in a session of
   its own, so nothing running as that user can type into root's terminal.
2. Every turn and every tool call is written to an audit log in `/var/lib/jarvis/audit`, each record
   chained to the one before it by its hash. What was said is kept as a length and a fingerprint only,
   unless `JARVIS_AUDIT_TEXT=on` is set in `/etc/jarvis/site.env`.
3. An action has happened only when a tool reported it. A reply that claims otherwise ("I have restarted
   it") is not shown: it goes back to the model once, and if the claim comes again it is cut and replaced
   by a plain sentence saying that nothing was done.
4. Jarvis does not answer with a bare no. A reply that refuses without saying what stands in the way and
   what can be done in its place goes back to the model once.
5. Who Jarvis is (`src/jarvis/personality.md`) is fixed text at the head of every conversation, followed
   by a description of what it can and cannot do that is built from the tools really present.

Jarvis's Python packages are named in `brain/requirements.txt`, each by version and checksum, and pip
installs nothing that does not match. They live in `/opt/jarvis-venv`, which only root can change; the
environment in use before an update is kept, so going back a release needs nothing from the internet.

## The desktop

The desktop container runs the [linuxserver.io Webtop](https://docs.linuxserver.io/images/docker-webtop/)
image (Ubuntu with XFCE, streamed to your browser) in Docker. When the desktop starts, two windows open and
stay in place: Jarvis's panel, docked on the right, and Jarvis's browser, filling the rest. They follow the
size of your browser window, and a window you close or minimize comes back by itself within a few
seconds. The panel has no title bar. Until the desktop is linked to a brain (see Jarvis's panel) it shows
a clock and says so; the browser opens with Jarvis's own start page, which is also what every new tab shows.

The desktop has no sign-in of its own, and whoever reaches it can type, click and read the screen. Put it
behind a reverse proxy that asks who you are, and tell the installer two things:

| Setting | What to give | Without it |
|---|---|---|
| `var_desktop_allow` | The address of your reverse proxy (several with commas between; a network by its first address, such as `192.0.2.16/28`) | Nobody can open the desktop |
| `var_desktop_origin` | The web address you open the desktop at, such as `https://desktop.example.org` | The picture connects only if your proxy passes the name you typed on to the desktop (the Host header); name the address to be sure |

The proxy forwards to `https://CONTAINER:3001` with WebSockets on, and must accept the certificate the
desktop made for itself. To change either setting later, run the installer again and name it
(`var_desktop_allow=192.0.2.7` before the command, or in the answers file); only what you name is changed.

What is done differently from the image's defaults, and why:

1. A firewall in the container drops everything that arrives, except for the named addresses on port
   3001. It is loaded before the network comes up, and Docker does not start without it.
2. The same firewall keeps the desktop's own user away from the desktop's ports. Jarvis's browser runs as
   that user, so a web page shown on the desktop cannot connect to the desktop's control port and drive
   it, nor to Jarvis's browser's DevTools port or the brain's door to it. (The web server in front of the
   control port runs as another user and still can, as does the door.) Jarvis's browser itself runs as a
   third user, which reaches only the internet (see "Jarvis's browser").
3. Chromium keeps its sandbox. Inside a Proxmox container the image's launchers start Chromium with
   `--no-sandbox`; Jarvis replaces them. After every install and update it checks that they are still
   replaced and that nothing runs with `--no-sandbox`, then starts the browser for a moment and looks at
   a page process for both layers of the sandbox (its own namespaces, its own system-call filter). For
   this, Docker runs the desktop without its own system-call filter (`seccomp=unconfined`); the filter of
   the Proxmox container stays.
4. The viewer's connection cannot run shell commands on the desktop, and the desktop's user cannot become
   root there (`SELKIES_COMMAND_ENABLED=false`, `DISABLE_SUDO=true`). The launcher in the viewer's side
   bar that starts programs does not work for that reason; the desktop's own menu does.
5. The image is named by its digest, so the desktop changes only with a release of Jarvis.
6. The desktop's own session must stay up. The image's programs load every picture through a loader that
   wants a `bwrap` sandbox, which Docker's AppArmor profile does not let it build; left alone, the
   desktop's bar and session stop on their first icon and are started over and over. Jarvis keeps Docker's
   profile and puts a stand-in in `bwrap`'s place that says such sandboxes are not available, which makes
   the loader work without one (`desktop/bwrap`). So pictures shown by the desktop's own programs (icons,
   the file manager's thumbnails) are read without that loader's sandbox. Pages in Chromium are not
   affected: its sandbox is its own. After every install the session is looked at once it has run long
   enough to tell (`desktop/session-check.sh`).

An install, update or `update --repair` that finds one of these not as it should be stops the desktop. To
look without changing anything: `bash /opt/jarvis/install/jarvis-desktop-install.sh --check`.

Chromium is told not to save passwords or form data, and is given Jarvis's colour and start page
(`desktop/policy.json`); open `chrome://policy` on the desktop to see that it took them.

What this does not do: pages in a browser you start yourself from the desktop's menu (as the desktop's user) can
reach whatever your network lets this container reach, so give it a network from which your other machines'
management pages are not reachable. Jarvis's browser cannot. Going
back to a release from before the desktop existed leaves Docker, the firewall and the desktop's
container in place.

## Update

Inside a container (`pct enter`, then):

```
update
```

It fetches the newest release, checks its signature, installs it, checks the result, and goes back to the
release it came from if that check fails. `update --check` only reports. `update --repair` puts the
installed release back as it was installed and runs its install steps again. If the code in `/opt/jarvis`
was changed by hand, both refuse until you add `--discard`. Whenever the code folder is put back in order
(with `--discard`, or after an install that was cut short), what differed is put aside in a git stash there
(`git -C /opt/jarvis stash list`), not dropped.

## What protects an installation

1. Releases are tags signed with an SSH key. The public key is copied from `trust/allowed_signers` into the
   container once, at install, and is never replaced from the repository. A tag counts as a release only if
   its name is `vX.Y.Z`, the tag itself carries that same name, its message starts with
   `Jarvis release vX.Y.Z`, and its SSH signature matches the pinned key; the code is then taken from the
   commit that signed tag names. A container records the newest release it has run and never moves to an
   older one. A release that was installed cannot be withdrawn from a container; publish a newer one.
2. Secrets are never part of this repository. Jarvis reads them from the folder you chose at install, and
   site settings from `/etc/jarvis/site.env`.
3. The two containers are separate so that a compromised web page in the desktop's browser does not sit
   next to the lab's credentials.

The installer on the Proxmox node is fetched over HTTPS from the address in the command, like other helper
scripts; read `ct/` and `misc/build.func` first if you want to know what will run as root. To take the
installer and the keys to pin from a fixed release, put its tag in the address in place of `main` and set
`JARVIS_REF` to the same tag; the container itself then installs the newest signed release.

## Development

```
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r brain/requirements.txt
.venv/bin/pip install pytest
.venv/bin/python -m pytest
```

The tests of Jarvis's own code (`tests/core`) need its packages, hence the environment; run with a Python
that lacks them, that folder is left out and the run says so. A model is never needed: Ollama is a
stand-in. `brain/requirements.in` says how the packages are locked.

The other tests run the installers and `update` for real, as root, against stand-ins for the Proxmox tools, on
private copies of the system folders (a mount namespace with overlays), so they change nothing on the
machine. They need root, `unshare`, git 2.34 or newer and `ssh-keygen`, and once the internet, to fetch the
packages the installer's pip is then given from a folder.

Three parts are tested against the real thing, and are skipped where it is missing: the firewall rules
against `nft` in network namespaces of their own; the sandbox check against a Chromium, with and without
its sandbox; and the window placement on XFCE's window manager on a virtual screen (`Xvfb`, `xfwm4`,
`xdotool`, a Chromium). Docker and the desktop image themselves are stand-ins in the tests.

Before the first commit in a clone, turn on the checks that keep secrets and site details out:

```
git config core.hooksPath scripts/hooks
```

`scripts/leak_gate.py` then runs before every commit, on merges, on the commit message and, before every
push, over what the remote does not hold yet (files, their names, messages, authors, tag and branch names). It refuses `.env` files, private network and hardware addresses, keys,
certificates, fingerprints and tokens. Examples use the documentation ranges (`192.0.2.x`,
`00:00:5E:00:53:xx`). Your own names (domain, hosts) go in a file of patterns outside the repository,
`~/.config/jarvis/leak-denylist`; a push is refused without one unless `JARVIS_LEAK_DENYLIST=none` says
there is nothing to protect. It knows shapes, not meanings, so it does not replace reading the diff, and it
does not run for edits made on GitHub itself.

A release is made by its owner, with an SSH key kept for this purpose only (here `~/.ssh/jarvis-release`):

```
git -c gpg.format=ssh -c user.signingkey=~/.ssh/jarvis-release.pub tag -s v1.2.3 -m "Jarvis release v1.2.3"
```

## License

MIT. The desktop is the linuxserver.io Webtop image, fetched from its publisher at install; it is not part
of this repository. The typeface of Jarvis's pages is Oxanium (`desktop/Oxanium.ttf`), under the SIL Open
Font License 1.1 (`desktop/Oxanium-LICENSE.txt`). The installer layout (a `ct/` script on the node, an `install/` script in the container, `var_`
settings, `update`) follows the Proxmox VE community helper scripts; no code of theirs is included.
