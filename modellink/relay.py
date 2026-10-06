"""Paired-device relay: Ollama traffic through the companion app.

The host never dials a paired device (it sits behind NAT). It queues an
ephemeral `kind: "ollama"` command on an in-memory queue; the companion
performs the local HTTP call against its own Ollama and posts seq-numbered
batches back to POST /api/agent/relay. RelayResponse mimics the slice of
requests.Response that splitter.py consumes (status_code, headers, iter_lines,
json, text, content), so the whole Ollama path — streaming, tools, vision,
errors — works unchanged.

Nothing here is persisted: relay commands never touch agents.json (chat
payloads must not land on disk), and abandoned streams are garbage-collected
after RELAY_GC_TTL. The companion learns a stream is dead when its next batch
gets HTTP 410 and aborts the local Ollama read.
"""
import json
import threading
import time

RELAY_FIRST_TIMEOUT = 45.0    # seconds for the companion's first message
RELAY_GC_TTL = 900.0          # drop orphaned streams after 15 minutes
_RELAY_ID_BASE = 10 ** 9      # keep ids clear of persisted command ids
_RELAY_BUFFER_MAX = 64 * 1024 * 1024


class RelayError(Exception):
    """User-facing relay failure. `ready` marks messages that are already
    complete sentences (raised by open()) and must not be re-wrapped."""

    def __init__(self, message, ready=False):
        super().__init__(message)
        self.ready = bool(ready)


class RelayTimeout(Exception):
    """The device went silent mid-stream."""


_lock = threading.Lock()
_streams = {}       # command id (str) -> RelayStream
_pending = {}       # agent id (str) -> [command dict] awaiting pickup
_ids = _RELAY_ID_BASE
_notifier = None    # callable(agent_id) -> wakes the agent's long-poll
_agent_info = None  # callable(agent_id) -> {"online","name","caps"} | None
_sweeper = False


def set_notifier(fn):
    global _notifier
    _notifier = fn


def set_agent_info(fn):
    global _agent_info
    _agent_info = fn


def _info(agent_id):
    if not _agent_info:
        return None
    try:
        return _agent_info(agent_id)
    except Exception:
        return None


def device_name(agent_id):
    info = _info(agent_id)
    if info and info.get("name"):
        return str(info["name"])
    return str(agent_id)


class RelayStream(object):
    def __init__(self, cid, agent_id, name):
        self.id = cid
        self.agent_id = str(agent_id)
        self.device_name = name
        self.created = time.time()
        self.last = time.time()
        self.seq_next = 0
        self.held = {}
        self.status = None
        self.headers = {}
        self.lines = []
        self.body = None
        self.done = False
        self.error = None
        self.cancelled = False
        self.buffered = 0
        self.first_timeout = RELAY_FIRST_TIMEOUT
        self.cond = threading.Condition()

    def _apply_locked(self, msg):
        st = msg.get("status")
        if st is not None:
            try:
                self.status = int(st)
            except (TypeError, ValueError):
                self.status = 0
        hdrs = msg.get("headers")
        if isinstance(hdrs, dict):
            self.headers = {str(k): str(v) for k, v in hdrs.items()}
        lines = msg.get("lines")
        if isinstance(lines, list):
            for line in lines:
                if isinstance(line, str):
                    self.lines.append(line)
                    self.buffered += len(line)
        body = msg.get("body")
        if isinstance(body, str):
            self.body = body
            self.buffered += len(body)
        err = msg.get("error")
        if err:
            self.error = str(err)[:500]
        if msg.get("done"):
            self.done = True
        if self.buffered > _RELAY_BUFFER_MAX and not self.error:
            self.error = "relay buffer overflow"


def open_relay(agent_id, path, body, first_timeout=None):
    """Queue an ephemeral ollama command for the device. Returns its
    RelayStream. Raises RelayError immediately when the device cannot serve
    it (unregistered, offline, companion too old)."""
    info = _info(agent_id)
    if not info:
        raise RelayError(
            "That paired device is no longer registered — pair it again from the Agents page.",
            ready=True)
    name = str(info.get("name") or agent_id)
    if not info.get("online"):
        raise RelayError(
            f"{name} is offline — open the Omniilink Server app on that device.",
            ready=True)
    if "ollama" not in (info.get("caps") or ()):
        raise RelayError(
            f"{name} runs an older companion app — update it from the Agents page "
            f"to use its models.", ready=True)
    global _ids
    _ensure_sweeper()
    with _lock:
        _ids += 1
        cid = str(_ids)
        stream = RelayStream(cid, agent_id, name)
        if first_timeout:
            try:
                stream.first_timeout = float(first_timeout)
            except (TypeError, ValueError):
                pass
        _streams[cid] = stream
        _pending.setdefault(str(agent_id), []).append(
            {"id": cid, "kind": "ollama", "timeout": 600,
             "payload": {"path": path, "body": body}})
    if _notifier:
        try:
            _notifier(agent_id)
        except Exception:
            pass
    return stream


def take_pending(agent_id):
    """Oldest queued relay command for the agent, or None."""
    with _lock:
        queue = _pending.get(str(agent_id))
        if queue:
            return queue.pop(0)
    return None


def attach(agent_id, msg):
    """Apply one batch from the companion. False (-> HTTP 410) means the
    stream is gone or belongs to another agent: the companion must abort."""
    if not isinstance(msg, dict):
        return False
    cid = str(msg.get("command_id", "") or "")
    if not cid:
        return False
    with _lock:
        stream = _streams.get(cid)
    if stream is None or stream.agent_id != str(agent_id):
        return False
    try:
        seq = int(msg.get("seq"))
    except (TypeError, ValueError):
        return False
    if seq < 0:
        return False
    with stream.cond:
        if stream.cancelled:
            return False
        stream.last = time.time()
        if seq < stream.seq_next:
            stream.cond.notify_all()
            return True
        stream.held[seq] = msg
        while stream.seq_next in stream.held:
            stream._apply_locked(stream.held.pop(stream.seq_next))
            stream.seq_next += 1
        stream.cond.notify_all()
    return True


def drop(cid):
    """Consumer finished or abandoned the stream: wake waiters and make the
    companion's next batch get 410 so it stops the local Ollama read."""
    with _lock:
        stream = _streams.pop(str(cid), None)
    if stream is not None:
        with stream.cond:
            stream.cancelled = True
            stream.cond.notify_all()


def drop_agent(agent_id):
    aid = str(agent_id)
    with _lock:
        stale = [cid for cid, s in _streams.items() if s.agent_id == aid]
        _pending.pop(aid, None)
    for cid in stale:
        drop(cid)


def sweep():
    now = time.time()
    with _lock:
        dead = [cid for cid, s in _streams.items()
                if now - s.last > RELAY_GC_TTL]
        for aid, queue in list(_pending.items()):
            _pending[aid] = [c for c in queue if str(c.get("id")) in _streams]
            if not _pending[aid]:
                _pending.pop(aid, None)
    for cid in dead:
        drop(cid)


def _ensure_sweeper():
    global _sweeper
    if _sweeper:
        return
    _sweeper = True

    def loop():
        while True:
            time.sleep(60)
            try:
                sweep()
            except Exception:
                pass
    threading.Thread(target=loop, daemon=True).start()


class RelayResponse(object):
    """requests.Response-alike fed by relay batches."""

    def __init__(self, stream, read_timeout=300.0):
        self._s = stream
        self._read_timeout = float(read_timeout)
        self.command_id = stream.id

    @property
    def device_name(self):
        return self._s.device_name

    @property
    def status_code(self):
        self._wait(lambda: self._s.status is not None)
        return self._s.status

    @property
    def headers(self):
        self._wait(lambda: self._s.status is not None)
        return dict(self._s.headers)

    @property
    def content(self):
        s = self._s
        self._wait(lambda: s.done and (s.body is not None or bool(s.lines)))
        if s.body is not None:
            body = s.body
            return body.encode("utf-8") if isinstance(body, str) else body
        return "\n".join(s.lines).encode("utf-8")

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.text)

    def iter_lines(self):
        s = self._s
        i = 0
        while True:
            with s.cond:
                line = None
                while True:
                    if i < len(s.lines):
                        line = s.lines[i]
                        i += 1
                        break
                    if s.error:
                        raise RelayError(s.error)
                    if s.cancelled:
                        raise RelayError(
                            f"{s.device_name} went offline (relay cancelled).")
                    if s.done:
                        return
                    now = time.time()
                    if s.seq_next == 0 and now - s.created > s.first_timeout:
                        raise RelayError(
                            f"{s.device_name} did not respond — the Omniilink "
                            f"Server app may be closed or busy.")
                    if now - s.last > self._read_timeout:
                        raise RelayTimeout(
                            f"{s.device_name} stopped responding "
                            f"(timed out after {int(self._read_timeout)}s).")
                    s.cond.wait(0.2)
            yield line.encode("utf-8") if isinstance(line, str) else line

    def _wait(self, pred):
        s = self._s
        with s.cond:
            while True:
                if pred():
                    return
                if s.cancelled:
                    raise RelayError(
                        f"{s.device_name} went offline (relay cancelled).")
                if s.error:
                    raise RelayError(s.error)
                now = time.time()
                if s.seq_next == 0:
                    if now - s.created > s.first_timeout:
                        raise RelayError(
                            f"{s.device_name} did not respond — the Omniilink "
                            f"Server app may be closed or busy.")
                elif now - s.last > self._read_timeout:
                    raise RelayTimeout(
                        f"{s.device_name} stopped responding "
                        f"(timed out after {int(self._read_timeout)}s).")
                s.cond.wait(0.2)

    def close(self):
        drop(self.command_id)


def open_response(agent_id, path, body, read_timeout=300.0, first_timeout=None):
    """Open the relay and return a RelayResponse for the splitter."""
    stream = open_relay(agent_id, path, body, first_timeout=first_timeout)
    return RelayResponse(stream, read_timeout=read_timeout)
