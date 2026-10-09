import json
import socket

from jarvis.audit import GENESIS, AuditLog, verify


def test_chain_appends_and_verifies(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    first = log.append("startup", {"v": 1})
    second = log.append("turn", {"session": "s"})
    assert first["prev"] == GENESIS
    assert second["prev"] == first["hash"]
    assert (first["seq"], second["seq"]) == (1, 2)
    assert verify(tmp_path / "a.jsonl") == (True, 2, "chain intact")


def test_resume_continues_the_chain(tmp_path):
    path = tmp_path / "a.jsonl"
    AuditLog(path).append("one", {})
    resumed = AuditLog(path)
    rec = resumed.append("two", {})
    assert rec["seq"] == 2
    assert verify(path)[0] is True


def test_edit_is_detected(tmp_path):
    path = tmp_path / "a.jsonl"
    log = AuditLog(path)
    log.append("tool_call", {"tool": "local_status"})
    log.append("turn", {"session": "s"})
    lines = path.read_text().splitlines()
    rec = json.loads(lines[0])
    rec["data"]["tool"] = "something_else"
    lines[0] = json.dumps(rec)
    path.write_text("\n".join(lines) + "\n")
    ok, count, message = verify(path)
    assert ok is False and count == 0 and "altered" in message


def test_deleted_line_is_detected(tmp_path):
    path = tmp_path / "a.jsonl"
    log = AuditLog(path)
    for i in range(3):
        log.append("turn", {"i": i})
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n")
    ok, count, message = verify(path)
    assert ok is False and count == 1 and "chain broken" in message


def test_syslog_copy_is_sent_as_rfc5424(tmp_path):
    recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    recv.bind(("127.0.0.1", 0))
    recv.settimeout(2)
    log = AuditLog(tmp_path / "a.jsonl", syslog=("127.0.0.1", recv.getsockname()[1]))
    rec = log.append("turn", {"session": "s", "user": "owner"})
    line = recv.recv(8192).decode()
    denied = log.append("request_denied", {"client": "192.0.2.99"})
    warn = recv.recv(8192).decode()
    recv.close()
    header, _, body = line.partition(" - turn - ")
    pri, stamp, host, app = header.split(" ")
    assert pri == "<134>1" and host == "jarvis" and app == "jarvis"
    assert stamp.endswith("Z") and len(stamp) == 24
    msg = json.loads(body)
    assert msg["audit_seq"] == 1 and msg["audit_hash"] == rec["hash"] and msg["user"] == "owner"
    assert warn.startswith("<132>1 ") and " - request_denied - " in warn and denied["seq"] == 2


def test_oversize_record_is_sent_as_a_stub(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl", syslog=("127.0.0.1", 9))
    rec = log.append("turn", {"reply_text": "x" * 20000})
    line = log.syslog_line(rec)
    assert len(line) < 400 and b'"truncated":true' in line


def test_syslog_failure_does_not_stop_the_log(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl", syslog=("256.0.0.1", 514))
    assert log.append("turn", {})["seq"] == 1


def _signer():
    import base64

    from jarvis.audit import Signer

    return Signer(base64.b64encode(bytes(range(32))).decode())


def test_signed_records_verify_with_the_public_key(tmp_path):
    signer = _signer()
    path = tmp_path / "a.jsonl"
    log = AuditLog(path, signer=signer)
    rec = log.append("turn", {"session": "s"})
    log.append("tool_call", {"tool": "dns_health"})
    assert len(rec["sig"]) == 88
    assert verify(path, signer.public_key) == (True, 2, "chain intact, 2 of 2 records signed, signatures valid")
    assert verify(path) == (True, 2, "chain intact, 2 of 2 records signed (signatures not checked: no public key)")


def test_wrong_key_and_forged_signature_are_detected(tmp_path):
    import base64

    from jarvis.audit import Signer

    path = tmp_path / "a.jsonl"
    AuditLog(path, signer=_signer()).append("turn", {})
    other = Signer(base64.b64encode(bytes(range(1, 33))).decode())
    ok, _, message = verify(path, other.public_key)
    assert ok is False and "signature does not match" in message

    # An attacker who can rewrite the file can redo the hash chain, but not the signature.
    forged = tmp_path / "forged.jsonl"
    log = AuditLog(forged, signer=_signer())
    log.append("turn", {"user": "owner"})
    rec = json.loads(forged.read_text())
    rec["data"]["user"] = "someone-else"
    from jarvis.audit import GENESIS, record_hash
    rec["hash"] = record_hash(GENESIS, {k: rec[k] for k in ("seq", "ts", "kind", "data")})
    forged.write_text(json.dumps(rec) + "\n")
    assert verify(forged)[0] is True, "the hash chain alone cannot see this"
    ok, _, message = verify(forged, _signer().public_key)
    assert ok is False and "signature does not match" in message


def test_unsigned_records_are_counted_and_strict_mode_refuses_late_ones(tmp_path):
    path = tmp_path / "a.jsonl"
    AuditLog(path).append("turn", {"n": 1})
    signer = _signer()
    AuditLog(path, signer=signer).append("turn", {"n": 2})
    assert verify(path, signer.public_key) == (True, 2, "chain intact, 1 of 2 records signed, signatures valid")
    AuditLog(path).append("startup", {"secrets": "locked"})
    ok, count, message = verify(path, signer.public_key)
    assert ok is True and count == 3
    assert message == "chain intact, 1 of 3 records signed, signatures valid; 1 unsigned after signing began (first at record 3)"
    ok, count, message = verify(path, signer.public_key, strict=True)
    assert ok is False and count == 2 and "unsigned record after signing began" in message


def test_bad_signing_keys_are_refused():
    import pytest

    from jarvis.audit import Signer

    for bad in ("", "not base64 !!", "c2hvcnQ="):
        with pytest.raises(ValueError):
            Signer(bad)


def test_signature_is_copied_to_syslog(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl", signer=_signer())
    rec = log.append("turn", {})
    assert f'"audit_sig":"{rec["sig"]}"' in log.syslog_line(rec).decode()


def test_two_writers_continue_one_chain(tmp_path):
    """The service and the jarvis command write the same log. Neither may continue from what it last wrote itself."""
    path = tmp_path / "a.jsonl"
    service, command = AuditLog(path), AuditLog(path)
    for n in range(6):
        (service if n % 2 else command).append("turn", {"n": n})
    command.append("turn", {"text": "x" * 20000})     # a record longer than one read from the end
    service.append("turn", {"n": 7})
    assert verify(path) == (True, 8, "chain intact") and service.seq == 8 and command.seq == 7


def test_many_writers_at_once(tmp_path):
    import multiprocessing

    path = tmp_path / "a.jsonl"
    AuditLog(path).append("startup", {})
    with multiprocessing.get_context("fork").Pool(4) as pool:
        pool.map(_write_some, [str(path)] * 4)
    assert verify(path) == (True, 101, "chain intact")


def _write_some(path):
    log = AuditLog(path)
    for n in range(25):
        log.append("turn", {"n": n})


def test_a_last_record_without_its_line_end_is_continued_on_a_new_line(tmp_path):
    path = tmp_path / "a.jsonl"
    AuditLog(path).append("turn", {"n": 1})
    path.write_bytes(path.read_bytes().rstrip(b"\n"))   # as an editor that strips the last line end leaves it
    AuditLog(path).append("turn", {"n": 2})
    AuditLog(path).append("turn", {"n": 3})
    assert verify(path) == (True, 3, "chain intact") and path.read_bytes().count(b"\n") == 3


def test_a_reader_never_sees_half_a_record(tmp_path):
    """Checking the log, or opening it to continue it, while another program writes to it."""
    import multiprocessing

    path = tmp_path / "a.jsonl"
    AuditLog(path).append("startup", {})
    writer = multiprocessing.get_context("fork").Process(target=_write_big, args=(str(path),))
    writer.start()
    try:
        for _ in range(150):
            good, _count, message = verify(path)
            assert good, message
            AuditLog(path)
    finally:
        writer.join()
    assert verify(path)[0]


def _write_big(path):
    log = AuditLog(path)
    for n in range(40):
        log.append("turn", {"n": n, "text": "x" * 300000})
