"""What Jarvis's browser may reach, as the brain judges it before the desktop's firewall does."""
from fakes import run

from jarvis.browser.cdp import Resolver, public

PUBLIC = "93.184.215.14"   # example.org's published address


def test_only_names_with_a_public_ipv4_address_and_nothing_private_are_allowed():
    names = {"web.test": [PUBLIC], "both.test": [PUBLIC, "2606:2800:21f:cb07:6820:80da:af6b:8b2c"],
             "six.test": ["2606:2800:21f:cb07:6820:80da:af6b:8b2c"], "mixed.test": [PUBLIC, "::1"],
             "mapped.test": [PUBLIC, "::ffff:127.0.0.1"], "nothing.test": []}

    async def lookup(host):
        return names[host]

    resolver = Resolver(lookup)
    verdicts = {host: run(resolver.allowed(f"https://{host}/")) for host in names}
    assert verdicts == {"web.test": True, "both.test": True, "six.test": False, "mixed.test": False,
                        "mapped.test": False, "nothing.test": False}
    assert not public("::ffff:127.0.0.1") and public("::ffff:" + PUBLIC)
    for url in ("http://[::1]/", "http://127.0.0.1/", "file:///etc/hostname", "javascript:void(0)", "http:///"):
        assert run(Resolver(lookup).allowed(url)) is False, url


def test_a_page_that_asks_for_too_many_names_gets_no_more_lookups():
    asked = []

    async def lookup(host):
        asked.append(host)
        return [PUBLIC]

    resolver = Resolver(lookup, max_hosts=3)
    assert [run(resolver.allowed(f"https://h{n}.test/")) for n in range(5)] == [True, True, True, False, False]
    assert len(asked) == 3 and resolver.capped
    assert run(resolver.allowed("https://h1.test/again")) is True, "a host already known is still answered"
