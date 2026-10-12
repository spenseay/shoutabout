"""The Mac window process plumbing: the JSON-lines link over real pipes, the hub-side
bridge (fake child process) and the child-side pairing/download proxies. No Tk window
is opened."""

from __future__ import annotations

import os
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from shoutabout.mac import uilink
from shoutabout.mac.ui_bridge import Downloads, MacUiBridge, child_command
from shoutabout.mac.ui_child import ChildUiHost, RemotePairing, remote_download
from shoutabout.mac.uilink import Link, LinkClosed, RemoteError
from shoutabout.pair_requests import PairPrompt


def pipe_pair():
    """Two Links joined by two OS pipes (like the hub and its child)."""
    a_read, b_write = os.pipe()
    b_read, a_write = os.pipe()
    a = (os.fdopen(a_read, "rb", buffering=0), os.fdopen(a_write, "wb", buffering=0))
    b = (os.fdopen(b_read, "rb", buffering=0), os.fdopen(b_write, "wb", buffering=0))
    return a, b


# --------------------------------------------------------------------- link


def test_encode_decode_round_trip_and_rejects_junk():
    msg = {"t": "pair", "device_name": "Sam’s iPhone", "code": "0421"}
    assert uilink.decode(uilink.encode(msg)) == msg
    assert uilink.encode(msg).endswith(b"\n")
    assert uilink.decode(b"not json\n") is None
    assert uilink.decode(b"[1, 2]\n") is None
    assert uilink.decode(b'{"no": "type"}\n') is None


def test_link_calls_events_and_errors():
    (ar, aw), (br, bw) = pipe_pair()
    events: queue.Queue = queue.Queue()

    def handle(op, args):
        if op == "add":
            return args["a"] + args["b"]
        raise ValueError("no such thing")

    hub = Link(ar, aw, on_call=handle, on_event=events.put)
    child = Link(br, bw)
    hub.start()
    child.start()
    try:
        assert child.call("add", a=2, b=3) == 5
        with pytest.raises(RemoteError, match="no such thing"):
            child.call("other")
        child.send({"t": "idle", "seq": 4})
        assert events.get(timeout=2) == {"t": "idle", "seq": 4}
    finally:
        child.close()
        hub.close()


def test_link_close_wakes_a_waiting_call():
    (ar, aw), (br, bw) = pipe_pair()
    closed = threading.Event()
    child = Link(br, bw, on_close=closed.set)
    child.start()
    result: list = []

    def caller():
        try:
            child.call("slow", timeout=5)
        except Exception as exc:
            result.append(exc)

    t = threading.Thread(target=caller)
    t.start()
    time.sleep(0.2)
    aw.close()  # the hub went away
    ar.close()
    t.join(3)
    assert closed.wait(2)
    assert result and isinstance(result[0], LinkClosed | ConnectionError | TimeoutError)
    assert not child.send({"t": "x"}) or child.closed


# ------------------------------------------------------------------- bridge


class FakePairing:
    def __init__(self):
        self.subscribed = []
        self.approved: list[str] = []
        self.denied: list[str] = []
        self.prompt = PairPrompt("req-1", "4821", "Sam's iPhone", 1e12)

    def subscribe(self, on_request=None, on_finished=None):
        self.subscribed.append((on_request, on_finished))

    def unsubscribe(self, cb):
        pass

    def seconds_left(self, prompt):
        return 99.0

    def pending(self):
        return self.prompt

    def approve(self, request_id):
        self.approved.append(request_id)
        return True

    def deny(self, request_id):
        self.denied.append(request_id)
        return True


class FakeChild:
    """A child process stand-in: its stdin/stdout are pipes we read and write."""

    def __init__(self, pump: bool = True):
        (self.hub_read, self.hub_write), (self.read, self.write) = pipe_pair()
        self.stdout, self.stdin = self.hub_read, self.hub_write
        self.lines: queue.Queue = queue.Queue()
        if pump:  # off when a test reads the pipe itself (two readers would race for lines)
            threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while True:
            line = self.read.readline()
            if not line:
                return
            self.lines.put(uilink.decode(line))

    def say(self, message):
        self.write.write(uilink.encode(message))
        self.write.flush()

    def next(self, kind, timeout=2.0):
        while True:
            msg = self.lines.get(timeout=timeout)
            if msg["t"] == kind:
                return msg

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return None


def make_bridge(tmp_path, *, pump: bool = True, **kw):
    pairing = FakePairing()
    children: list[FakeChild] = []

    def spawn(argv):
        child = FakeChild(pump=pump)
        children.append(child)
        return child

    saved: list = []
    services = SimpleNamespace(
        paths=SimpleNamespace(home=tmp_path, log_dir=tmp_path / "logs"),
        pairing=pairing,
        identity=SimpleNamespace(reload=lambda: saved.append("reload")),
        on_settings_saved=lambda keys: {k for k in keys if k.startswith("stt.")},
    )
    bridge = MacUiBridge(services, spawn=spawn, **kw)
    return bridge, pairing, children, saved


def test_child_command(tmp_path):
    argv = child_command(SimpleNamespace(home=tmp_path))
    assert argv[-3:] == ["--home", str(tmp_path), "mac-ui"]


def test_bridge_starts_the_window_process_on_demand(tmp_path):
    bridge, _pairing, children, _ = make_bridge(tmp_path)
    assert children == []
    bridge.open_settings()
    assert len(children) == 1
    msg = children[0].next("open")
    assert msg["kind"] == "settings" and msg["seq"] == 1
    bridge.open_setup("phone")
    assert children[0].next("open")["arg"] == "phone"
    assert len(children) == 1  # reused
    bridge.shutdown(0.1)


def test_bridge_quits_an_idle_child_only_when_it_saw_everything(tmp_path):
    bridge, _pairing, children, _ = make_bridge(tmp_path)
    bridge.open_settings()
    child = children[0]
    child.next("open")
    bridge.open_setup()  # seq 2 is on its way
    child.say({"t": "idle", "seq": 1})  # the child hasn't handled seq 2 yet
    time.sleep(0.2)
    assert bridge.running
    child.next("open")
    child.say({"t": "idle", "seq": 2})
    assert child.next("quit")
    assert not bridge.running
    bridge.open_settings()  # a new request starts a new process
    assert len(children) == 2
    bridge.shutdown(0.1)


def test_bridge_answers_the_childs_calls(tmp_path):
    bridge, pairing, children, saved = make_bridge(tmp_path, pump=False)
    bridge.open_settings()
    link = Link(children[0].read, children[0].write)  # the only reader of the child's pipe
    link.start()
    try:
        assert link.call("approve", request_id="req-1") is True
        assert pairing.approved == ["req-1"]
        assert link.call("deny", request_id="req-2") is True
        restart = link.call("settings_saved", keys=["stt.tier", "dictation.mode"])
        assert restart == ["stt.tier"]
        assert link.call("identity_changed") is None and saved == ["reload"]
        with pytest.raises(RemoteError, match="unknown"):
            link.call("format_disk")
        with pytest.raises(RemoteError, match="permissions"):
            link.call("permissions")  # no permissions to ask for (not a Mac hub)
    finally:
        link.close()
        bridge.shutdown(0.1)


def test_bridge_reads_and_asks_for_permissions_in_the_hub(tmp_path):
    """The setup's "Allow access" runs in the window process; the hub asks macOS."""
    from v2_fakes import FakeAccess

    bridge, _pairing, children, _ = make_bridge(tmp_path, pump=False)
    access = FakeAccess(grant_on_ask=True, answer="asked")
    bridge.svc.permissions = access
    bridge.open_setup()
    link = Link(children[0].read, children[0].write)
    link.start()
    try:
        assert link.call("permissions")["microphone"] == "undetermined"
        assert link.call("allow", which="microphone") == "asked"
        assert access.asked == ["microphone"]
        assert link.call("permissions")["microphone"] == "granted"
    finally:
        link.close()
        bridge.shutdown(0.1)


def test_bridge_forwards_pairing_and_falls_back_to_a_native_prompt(tmp_path):
    asked: list = []
    bridge, pairing, children, _ = make_bridge(
        tmp_path, native_prompt=asked.append, ack_timeout=0.3
    )
    on_request, on_finished = pairing.subscribed[0]
    on_request(pairing.prompt)
    msg = children[0].next("pair")
    assert msg["code"] == "4821" and msg["seconds_left"] == 99.0
    assert "token" not in msg
    time.sleep(0.6)  # no ack from the child
    assert asked == [pairing.prompt]
    on_finished("req-1", "approved")
    assert children[0].next("pair-done")["status"] == "approved"
    bridge.shutdown(0.1)


def test_bridge_no_native_prompt_when_the_child_acks(tmp_path):
    asked: list = []
    bridge, pairing, children, _ = make_bridge(
        tmp_path, native_prompt=asked.append, ack_timeout=1.0
    )
    pairing.subscribed[0][0](pairing.prompt)
    children[0].next("pair")
    children[0].say({"t": "ack", "request_id": "req-1"})
    time.sleep(1.2)
    assert asked == []
    bridge.shutdown(0.1)


def test_downloads_run_once_and_report_progress():
    calls: list = []
    gate = threading.Event()

    def download(model, *, progress, cancel):
        calls.append(model)
        progress(50, 100)
        gate.wait(2)
        progress(100, 100)

    d = Downloads(download)
    assert d.status("small.en")["state"] == "idle"
    d.start("small.en")
    d.start("small.en")
    time.sleep(0.1)
    assert d.status("small.en") == {"state": "running", "done": 50, "total": 100, "error": None}
    gate.set()
    for _ in range(50):
        if d.status("small.en")["state"] == "ready":
            break
        time.sleep(0.02)
    assert d.status("small.en")["state"] == "ready" and calls == ["small.en"]


def test_downloads_report_failures():
    def download(model, *, progress, cancel):
        raise OSError("no internet")

    d = Downloads(download)
    d.start("base.en")
    for _ in range(50):
        if d.status("base.en")["state"] == "failed":
            break
        time.sleep(0.02)
    assert d.status("base.en")["error"] == "no internet"


# -------------------------------------------------------------------- child


class FakeLink:
    def __init__(self, answers=None):
        self.calls: list = []
        self.sent: list = []
        self.answers = answers or {}

    def call(self, op, **args):
        self.calls.append((op, args))
        answer = self.answers.get(op)
        return answer(args) if callable(answer) else answer

    def send(self, message):
        self.sent.append(message)
        return True


def test_remote_pairing_tracks_prompts_and_asks_the_hub():
    clock = [1000.0]
    link = FakeLink({"approve": True, "deny": True})
    pairing = RemotePairing(link, clock=lambda: clock[0])
    seen, done = [], []
    pairing.subscribe(seen.append, lambda rid, status: done.append((rid, status)))
    prompt = pairing.add("req-1", "4821", "Sam's iPhone", 60)
    assert seen == [prompt] and pairing.pending() == prompt
    assert pairing.seconds_left(prompt) == 60
    clock[0] += 61
    assert pairing.pending() is None
    assert pairing.approve("req-1") is True
    assert link.calls[-1] == ("approve", {"request_id": "req-1"})
    pairing.finish("req-1", "approved")
    assert done == [("req-1", "approved")]


def test_remote_download_polls_the_hub():
    statuses = iter(
        [{"state": "running", "done": 10, "total": 100}, {"state": "ready", "done": 100}]
    )
    link = FakeLink({"download_status": lambda args: next(statuses)})
    progress: list = []
    remote_download(link)(
        "small.en", progress=lambda d, t: progress.append((d, t)), cancel=threading.Event()
    )
    assert link.calls[0] == ("download", {"model": "small.en"})
    assert progress[0] == (10, 100)

    failing = FakeLink({"download_status": {"state": "failed", "error": "disk full"}})
    with pytest.raises(RuntimeError, match="disk full"):
        remote_download(failing)("x", progress=lambda d, t: None, cancel=threading.Event())


def test_child_host_queues_hub_messages_and_acks_pairing():
    link = FakeLink()
    pairing = RemotePairing(link)
    services = SimpleNamespace(pairing=pairing)
    host = ChildUiHost(services, link, pairing)
    host.on_event({"t": "open", "kind": "settings", "seq": 1})
    host.on_event({"t": "open", "kind": "setup", "arg": "phone", "seq": 2})
    host.on_event(
        {
            "t": "pair",
            "request_id": "r",
            "code": "1234",
            "device_name": "P",
            "seconds_left": 30,
            "seq": 3,
        }
    )
    host.on_event({"t": "pair-done", "request_id": "r", "status": "denied", "seq": 4})
    host.on_event({"t": "quit", "seq": 5})
    queued = []
    while not host._requests.empty():
        queued.append(host._requests.get_nowait())
    kinds = [k for k, _ in queued]
    assert kinds == ["settings", "setup", "pair", "pair-done", "quit"]
    assert queued[1][1] == "phone"
    assert queued[2][1].code == "1234"
    assert link.sent == [{"t": "ack", "request_id": "r"}]
    assert host._last_seq == 5
    host.on_link_closed()
    assert host._requests.get_nowait() == ("quit", None)
