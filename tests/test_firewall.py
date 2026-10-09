"""The containers' firewall rules, loaded into the real nft in a network of their own.

The rules are the ones the install script writes (taken from a test install). Two private network
namespaces joined by a cable stand for the container and for the machines around it. Needs root, ip and nft.
"""
import os
import re
import shutil
import socket
import subprocess
import sys
import time

import pytest

SERVER = """
import socket, sys, threading
def serve(port):
    try:
        s = socket.socket(socket.AF_INET6)
        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)   # answers on IPv4 and IPv6 alike
        where = ("::", port)
    except OSError:                                                 # a machine without IPv6
        s = socket.socket()
        where = ("0.0.0.0", port)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(where); s.listen(8)
    while True:
        c, _ = s.accept(); c.close()
for port in map(int, sys.argv[1:]):
    threading.Thread(target=serve, args=(port,), daemon=True).start()
print("ready", flush=True)
threading.Event().wait()
"""
CONNECT = ("import socket, sys; source = (sys.argv[3], 0) if len(sys.argv) > 3 else None; "
           "socket.create_connection((sys.argv[1], int(sys.argv[2])), 1.5, source_address=source).close()")
REAL = "/usr/sbin:/usr/bin:/sbin:/bin"


def sh(*args, check=True):
    return subprocess.run([str(a) for a in args], check=check, text=True, env={"PATH": REAL},
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


@pytest.fixture
def network():
    if os.geteuid() != 0 or not shutil.which("ip", path=REAL) or not shutil.which("nft", path=REAL):
        pytest.skip("needs root, ip and nft")
    desk, world = f"jarvis-t-desk-{os.getpid()}", f"jarvis-t-world-{os.getpid()}"
    if sh("ip", "netns", "add", desk, check=False).returncode != 0:
        pytest.skip("network namespaces cannot be made here")
    servers = []
    try:
        sh("ip", "netns", "add", world)
        sh("ip", "link", "add", "vd", "netns", desk, "type", "veth", "peer", "name", "vw", "netns", world)
        for command in (f"ip -n {desk} addr add 192.0.2.1/24 dev vd", f"ip -n {desk} link set vd up", f"ip -n {desk} link set lo up",
                        f"ip -n {desk} route add 198.51.100.0/24 dev vd",
                        f"ip -n {world} addr add 192.0.2.7/24 dev vw", f"ip -n {world} addr add 192.0.2.8/24 dev vw",
                        f"ip -n {world} addr add 198.51.100.9/24 dev vw", f"ip -n {world} addr add 203.0.113.5/24 dev vw",
                        f"ip -n {world} addr add 198.18.0.9/24 dev vw", f"ip -n {desk} route add 198.18.0.0/24 dev vd",
                        f"ip -n {world} link set vw up", f"ip -n {world} link set lo up",
                        f"ip -n {desk} route add 203.0.113.0/24 dev vd"):
            sh(*command.split())
        for command in (f"ip -n {desk} addr add 2001:db8::1/64 dev vd nodad", f"ip -n {world} addr add 2001:db8::7/64 dev vw nodad"):
            sh(*command.split(), check=False)   # a machine without IPv6: that leg is then not tested
        if sh("ip", "netns", "exec", desk, "nft", "list", "tables", check=False).returncode != 0:
            pytest.skip("nft cannot talk to the kernel here")
        for ns, ports in ((desk, ("3000", "3001", "8082", "8443", "22", "9222", "9223")), (world, ("8080", "53"))):
            server = subprocess.Popen(["ip", "netns", "exec", ns, sys.executable, "-c", SERVER, *ports], env={"PATH": REAL},
                                      stdout=subprocess.PIPE, text=True)
            assert server.stdout.readline().strip() == "ready"
            servers.append(server)
        time.sleep(1.5)  # the addresses of a new cable need a moment before they may be used as a source

        def reach(source, port, ns=world, target="192.0.2.1"):
            return sh("ip", "netns", "exec", ns, sys.executable, "-c", CONNECT, target, port, source, check=False).returncode == 0

        def local(uid, target, port):
            """A program of that user, inside the container, connects to one of the container's own addresses."""
            return sh("ip", "netns", "exec", desk, "setpriv", "--reuid", uid, "--regid", uid, "--clear-groups",
                      sys.executable, "-c", CONNECT, target, port, check=False).returncode == 0

        reach.local = local
        yield desk, world, reach
    finally:
        for server in servers:
            server.kill()
        sh("ip", "netns", "del", desk, check=False)
        sh("ip", "netns", "del", world, check=False)


def test_only_the_named_addresses_reach_only_the_desktops_port(bench, network, tmp_path):
    desk, world, reach = network
    bench.release("v0.1.0")
    done = bench.install("desktop", bench.answers("desktop", desktop_allow="192.0.2.7,198.51.100.0/24"))
    assert done.returncode == 0, done.stdout
    rules = tmp_path / "desktop.nft"
    rules.write_text(bench.read("/etc/jarvis/desktop.nft"))

    # Before the rules: everything is reachable, so what follows is the rules' doing.
    assert reach("192.0.2.8", 3001) and reach("203.0.113.5", 3000) and reach("192.0.2.8", 22)

    assert sh("ip", "netns", "exec", desk, "nft", "-c", "-f", rules).returncode == 0
    sh("ip", "netns", "exec", desk, "nft", "-f", rules)
    sh("ip", "netns", "exec", desk, "nft", "-f", rules)  # loading again replaces the table, it does not double it
    listed = sh("ip", "netns", "exec", desk, "nft", "list", "ruleset").stdout
    assert listed.count("table inet jarvis_desktop") == 1 and len(re.findall(r"tcp dport 3001 counter packets \d+ bytes \d+ accept", listed)) == 1

    assert reach("192.0.2.7", 3001)                 # the named address
    assert reach("198.51.100.9", 3001)              # an address of the named network
    assert not reach("192.0.2.8", 3001)             # its neighbour
    assert not reach("203.0.113.5", 3001)
    for port in (3000, 8082, 22):                   # the desktop's other ports, and anything else: for nobody
        assert not reach("192.0.2.7", port), port
        assert not reach("198.51.100.9", port), port
    assert sh("ip", "netns", "exec", world, "ping", "-c", "1", "-W", "2", "-I", "192.0.2.8", "192.0.2.1", check=False).returncode == 0
    assert reach("192.0.2.1", 8080, ns=desk, target="192.0.2.7")   # what the container itself asks for is answered
    assert reach("127.0.0.1", 3000, ns=desk, target="127.0.0.1")   # and it reaches its own ports

    sh("ip", "netns", "exec", desk, "nft", "delete", "table", "inet", "jarvis_desktop")
    assert reach("192.0.2.8", 3001) and reach("192.0.2.7", 3000)


def test_with_nobody_named_nothing_gets_in(bench, network, tmp_path):
    desk, world, reach = network
    bench.release("v0.1.0")
    assert bench.install("desktop").returncode == 0
    rules = tmp_path / "desktop.nft"
    rules.write_text(bench.read("/etc/jarvis/desktop.nft"))
    sh("ip", "netns", "exec", desk, "nft", "-f", rules)
    for source in ("192.0.2.7", "192.0.2.8", "198.51.100.9"):
        for port in (3000, 3001, 22):
            assert not reach(source, port), (source, port)
    assert reach("192.0.2.1", 8080, ns=desk, target="192.0.2.7")


def test_a_page_in_the_desktops_browser_cannot_reach_the_desktops_own_ports(bench, network, tmp_path):
    """The desktop's control port would let whoever connects type, click and read the screen. The browser
    runs as the desktop's user (1000); the web server in front of the control port runs as another."""
    desk, world, reach = network
    if not shutil.which("setpriv", path=REAL):
        pytest.skip("needs setpriv")
    bench.release("v0.1.0")
    done = bench.install("desktop", bench.answers("desktop", desktop_allow="192.0.2.7"))
    assert done.returncode == 0, done.stdout
    rules = tmp_path / "desktop.nft"
    rules.write_text(bench.read("/etc/jarvis/desktop.nft"))
    own = ["127.0.0.1", "192.0.2.1"]              # loopback, and the container's address on the network
    try:
        socket.socket(socket.AF_INET6).close()
        own.append("::1")
    except OSError:
        pass                                        # this machine has no IPv6; that leg is then not tested

    for target in own:                              # before the rules, the desktop's user reaches everything
        assert reach.local(1000, target, 8082), target
    sh("ip", "netns", "exec", desk, "nft", "-f", rules)

    for target in own:
        for port in (3000, 3001, 8082):
            assert not reach.local(1000, target, port), (target, port)   # the browser's user: no
            assert reach.local(33, target, port), (target, port)         # the web server's user: yes
            assert reach.local(0, target, port), (target, port)          # root, for the install's own checks: yes
        assert reach.local(1000, target, 22), target                     # other ports of the container: untouched
    assert reach.local(1000, "192.0.2.7", 8080)                          # and the browser still browses
    assert reach("192.0.2.7", 3001)                                      # while the named proxy still gets in
    counted = sh("ip", "netns", "exec", desk, "nft", "list", "table", "inet", "jarvis_desktop").stdout
    assert re.search(r"meta skuid 1000 .* counter packets [1-9]", counted), counted


def test_jarviss_browser_reaches_the_internet_and_the_name_servers_and_nothing_private(bench, network, tmp_path):
    """Jarvis's browser runs as user 1001. Here 192.0.2.7 stands for the internet, and 198.18.0.9 (a range kept
    from the browser) for the lab, whose machine is also the name server."""
    desk, world, reach = network
    if not shutil.which("setpriv", path=REAL):
        pytest.skip("needs setpriv")
    bench.release("v0.1.0")
    bench.sh("printf '%s\\n' 'nameserver 198.18.0.9' > /etc/resolv.conf && rm -f /run/systemd/resolve/resolv.conf")
    done = bench.install("desktop", bench.answers("desktop", desktop_allow="192.0.2.7"))
    assert done.returncode == 0, done.stdout
    rules = tmp_path / "desktop.nft"
    rules.write_text(bench.read("/etc/jarvis/desktop.nft"))
    assert reach.local(1001, "198.18.0.9", 8080) and reach.local(1001, "127.0.0.1", 3001)   # before the rules
    six = reach.local(1001, "2001:db8::7", 8080)
    sh("ip", "netns", "exec", desk, "nft", "-f", rules)

    assert reach.local(1001, "192.0.2.7", 8080)                  # the internet
    assert reach.local(1001, "198.18.0.9", 53)                   # the name server, for names only
    assert not reach.local(1001, "198.18.0.9", 8080)             # the lab
    for target, port in (("127.0.0.1", 3001), ("127.0.0.1", 9222), ("192.0.2.1", 22), ("192.0.2.1", 8443)):
        assert not reach.local(1001, target, port), (target, port)   # the desktop's container itself
    assert reach.local(1000, "198.18.0.9", 8080), "the desktop's own user is not limited this way"
    if six:   # no IPv6 at all for that user, whatever the address: a lab's machines have global ones too
        assert not reach.local(1001, "2001:db8::7", 8080) and reach.local(1000, "2001:db8::7", 8080)
    # DevTools listens as that user; its answers to the door (another user, on loopback) go out.
    listener = subprocess.Popen(["ip", "netns", "exec", desk, "setpriv", "--reuid", "1001", "--regid", "1001", "--clear-groups",
                                 sys.executable, "-c", SERVER, "9333"], env={"PATH": REAL}, stdout=subprocess.PIPE, text=True)
    try:
        assert listener.stdout.readline().strip() == "ready"
        assert reach.local(999, "127.0.0.1", 9333)
    finally:
        listener.kill()


def test_only_the_brain_reaches_the_browsers_door_and_no_page_reaches_the_browsers_devtools(bench, network, tmp_path):
    """DevTools gives whoever reaches it the whole browser. From outside only the brain's address gets to the door
    (9223), and nobody to DevTools itself (9222); from inside, the desktop's user (every page) gets to neither,
    while the door's own user reaches DevTools on loopback."""
    from test_service_install import a_brain
    desk, world, reach = network
    if not shutil.which("setpriv", path=REAL):
        pytest.skip("needs setpriv")
    bench.release("v0.1.0")
    a_brain(bench, address="192.0.2.8")
    done = bench.install("desktop", bench.answers("desktop", desktop_allow="192.0.2.7", brain="190"))
    assert done.returncode == 0, done.stdout
    rules = tmp_path / "desktop.nft"
    rules.write_text(bench.read("/etc/jarvis/desktop.nft"))
    assert reach("192.0.2.7", 9223) and reach("192.0.2.8", 9222)   # before the rules, everybody
    sh("ip", "netns", "exec", desk, "nft", "-f", rules)

    assert reach("192.0.2.8", 9223)                  # the brain, to the door
    for source in ("192.0.2.7", "198.51.100.9", "203.0.113.5"):
        assert not reach(source, 9223), source        # the proxy and everybody else: no
    for source in ("192.0.2.7", "192.0.2.8"):
        assert not reach(source, 9222), source        # DevTools itself: nobody from outside
        assert not reach("192.0.2.8", 3001) and reach("192.0.2.7", 3001)
    for target in ("127.0.0.1", "192.0.2.1"):
        for port in (9222, 9223):
            assert not reach.local(1000, target, port), (target, port)   # a page in the desktop's browser: no
    assert reach.local(999, "127.0.0.1", 9222)        # the door's user: yes


def test_only_the_named_addresses_reach_jarviss_port_and_nothing_else_about_the_brain_changes(bench, network, tmp_path):
    """Here the first namespace stands for the brain container. Its rule decides about Jarvis's port alone."""
    brain, world, reach = network
    bench.release("v0.1.0")
    done = bench.install("brain", bench.answers(panel_allow="192.0.2.7,198.51.100.0/24"))
    assert done.returncode == 0, done.stdout
    rules = tmp_path / "brain.nft"
    rules.write_text(bench.read("/etc/jarvis/brain.nft"))
    assert reach("192.0.2.8", 8443) and reach("192.0.2.8", 22)

    assert sh("ip", "netns", "exec", brain, "nft", "-c", "-f", rules).returncode == 0
    sh("ip", "netns", "exec", brain, "nft", "-f", rules)
    sh("ip", "netns", "exec", brain, "nft", "-f", rules)   # loading again replaces the table
    assert sh("ip", "netns", "exec", brain, "nft", "list", "ruleset").stdout.count("table inet jarvis_brain") == 1

    assert reach("192.0.2.7", 8443)                  # the desktop
    assert reach("198.51.100.9", 8443)               # an address of the named network
    assert not reach("192.0.2.8", 8443)              # its neighbour
    assert not reach("203.0.113.5", 8443)
    for source in ("192.0.2.7", "192.0.2.8", "203.0.113.5"):
        assert reach(source, 22), source              # every other port of the brain: as before
    assert reach("192.0.2.1", 8443, ns=brain, target="192.0.2.1")    # the brain's own check, by its own address
    assert reach("127.0.0.1", 8443, ns=brain, target="127.0.0.1")
    assert reach("192.0.2.1", 8080, ns=brain, target="192.0.2.7")    # and what the brain itself asks for
    counted = sh("ip", "netns", "exec", brain, "nft", "list", "table", "inet", "jarvis_brain").stdout
    assert re.search(r"tcp dport 8443 counter packets [1-9]\d* bytes \d+ drop", counted), counted

    # With nobody named, nobody but the brain itself.
    again = bench.install("brain", bench.answers(panel_allow=""))
    assert again.returncode == 0, again.stdout
    rules.write_text(bench.read("/etc/jarvis/brain.nft"))
    sh("ip", "netns", "exec", brain, "nft", "-f", rules)
    assert not reach("192.0.2.7", 8443) and reach("192.0.2.7", 22) and reach("192.0.2.1", 8443, ns=brain, target="192.0.2.1")
