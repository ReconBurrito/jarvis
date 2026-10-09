"""The deploy key reaches ssh through a private agent and is never written to a file."""
import asyncio
import os
import shutil

import asyncssh
import pytest
from fakes import run

from jarvis.notes.agent import AgentError, KeyAgent

pytestmark = pytest.mark.skipif(not (shutil.which("ssh-agent") and shutil.which("ssh-add") and shutil.which("ssh")),
                                reason="needs OpenSSH's ssh, ssh-agent and ssh-add")


def test_the_key_lives_in_the_agent_only_and_ssh_logs_in_with_it(tmp_path):
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    secret = client_key.export_private_key().decode()

    async def go():
        host_key = asyncssh.generate_private_key("ssh-ed25519")
        allowed = client_key.convert_to_public()

        class Server(asyncssh.SSHServer):
            def begin_auth(self, username):
                return True

            def public_key_auth_supported(self):
                return True

            def validate_public_key(self, username, key):
                return key == allowed

        server = await asyncssh.create_server(Server, "127.0.0.1", 0, server_host_keys=[host_key],
                                              process_factory=lambda p: (p.stdout.write("in\n"), p.exit(0)))
        port = server.sockets[0].getsockname()[1]
        known = tmp_path / "known_hosts"
        known.write_text(f"[127.0.0.1]:{port} {host_key.export_public_key().decode()}")
        agent = KeyAgent()
        socket = await agent.start(secret.encode())
        folder = socket.parent
        held = sorted(os.listdir(folder))
        mode = oct(os.stat(folder).st_mode & 0o777)
        ssh = await asyncio.create_subprocess_exec(
            "ssh", "-F", "/dev/null", "-o", f"IdentityAgent={socket}", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={known}", "-o", "GlobalKnownHostsFile=/dev/null", "-p", str(port), "t@127.0.0.1", "x",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"})
        out, err = await ssh.communicate()
        await agent.stop()
        server.close()
        return held, mode, ssh.returncode, out, folder

    held, mode, code, out, folder = run(go())
    assert code == 0 and out == b"in\n"
    assert held == ["socket"] and mode == "0o700", "the folder holds the agent's socket and nothing else"
    assert not folder.exists(), "stopping the agent leaves nothing behind"


def test_a_public_key_in_place_of_the_private_one_is_refused():
    public = asyncssh.generate_private_key("ssh-ed25519").export_public_key()
    with pytest.raises(AgentError, match="not a private key"):
        run(KeyAgent().start(public))
