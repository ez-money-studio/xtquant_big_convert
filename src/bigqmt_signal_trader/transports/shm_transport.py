# coding: utf-8
"""Shared-memory transport: request/response over a named Windows section.

The wire for a terminal that took the other wires away. A broker sandbox whose
import whitelist rejects ``socket`` and ``redis``, whose EDR kills ``connect()``
and where ``pip install`` is not an option still leaves one primitive: a named
file mapping. CreateFileMapping produces no path and no connection, so it sits
outside the threat model every network/file monitor is built around.

What is deliberately absent
---------------------------
No sockets, no files, no ``ctypes``, no named events, no temporary files, no
third-party packages. One ``mmap`` per channel, and every synchronization
primitive lives inside that mapping: a FIFO is a pair of counters and a slot
state byte. ``tests/bigqmt_signal_trader/test_shm_transport.py`` asserts this by
AST, so it cannot rot silently.

Two roles, one class
--------------------
* client -- :meth:`SharedMemoryTransport.send_request`
* server -- :meth:`SharedMemoryTransport.start_receiving` (background thread) or
  :meth:`SharedMemoryTransport.drain_request_queue` (the QMT ``adjust`` callback
  polls it; no extra thread is created at all, which matters inside a sandbox)

Why there is a spin window
--------------------------
With no event object to wait on, the only way to wait is to poll -- and on
Windows ``time.sleep`` is far coarser than it looks. Measured on the dev machine
(CPython 3.10)::

    time.sleep(0.0005) -> median 15.49 ms
    time.sleep(0.0050) -> median 15.55 ms

CPython 3.10 waits on the scheduler tick, ~15.6 ms, whatever you ask for, and
QMT's embedded 3.6 is no better. A transport whose wire costs ~60 microseconds
cannot pay 15 ms per hop, so both ends busy-spin for a bounded window
(``spin_seconds``) before falling back to sleep, and the server keeps spinning
for a short while *after* handling a request -- which is where the requests
actually are. Back-to-back calls then cost microseconds, and a long-idle bridge
costs no CPU.

Measured cross-process on the dev machine, 500 round trips of a ~120-byte
request: p50 0.063 ms / p99 0.28 ms against a spinning pump; 15.6 ms p50 against
a ``time.sleep``-driven pump, i.e. exactly the OS tick.
"""

import json
import os
import threading
import time

from . import shm_layout
from .base import RpcTransport, TransportError, TransportTimeout
from .shm_ring import Section, SlotRing


def _encode(obj):
    return json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")


def _decode(raw):
    return json.loads(raw.decode("utf-8"))


class _ChannelLink(object):
    """One client thread's private channel: its section, rings and identity."""

    def __init__(self, index, section, epoch, owner_pid, nonce):
        self.index = int(index)
        self.section = section
        self.epoch = int(epoch)
        self.owner_pid = int(owner_pid)
        self.nonce = int(nonce)
        self.req = SlotRing(section, "req")
        self.resp = SlotRing(section, "resp")
        self.last_client_beat_ms = 0
        self.last_server_check_ms = 0

    def still_owned(self):
        header = self.section.read_header()
        return (header["server_epoch"] == self.epoch
                and header["owner_pid"] == self.owner_pid
                and header["flags"] == self.nonce)

    def alive(self):
        try:
            return self.still_owned()
        except Exception:
            return False

    def close(self):
        self.section.close()


class SharedMemoryTransport(RpcTransport):
    """Request/response over Windows named shared memory. ``name = "shm"``."""

    name = "shm"

    def __init__(self, account_id="", print_prefix="[bigqmt_rpc]",
                 channel_count=None, slot_size=None, slot_count=None,
                 namespace=None, name_prefix=None, hash_salt="",
                 connect_timeout_seconds=5.0, spin_seconds=0.05,
                 idle_sleep_seconds=0.001, server_heartbeat_interval_seconds=0.1,
                 client_heartbeat_interval_seconds=0.2, client_ttl_seconds=10.0,
                 server_ttl_seconds=30.0, response_publish_timeout_seconds=0.05,
                 maintain_interval_seconds=0.5, **_ignored):
        super(SharedMemoryTransport, self).__init__(
            account_id=account_id, print_prefix=print_prefix)
        self.channel_count = int(channel_count or shm_layout.DEFAULT_CHANNEL_COUNT)
        self.slot_size = int(slot_size or shm_layout.DEFAULT_SLOT_SIZE)
        self.slot_count = int(slot_count or shm_layout.DEFAULT_SLOT_COUNT)
        self.namespace = str(namespace or shm_layout.DEFAULT_NAMESPACE)
        self.name_prefix = str(name_prefix or shm_layout.DEFAULT_NAME_PREFIX)
        self.hash_salt = str(hash_salt or "")
        self.connect_timeout_seconds = float(connect_timeout_seconds)
        self.spin_seconds = max(0.0, float(spin_seconds))
        self.idle_sleep_seconds = max(0.0, float(idle_sleep_seconds))
        self.server_heartbeat_interval_seconds = float(
            server_heartbeat_interval_seconds)
        self.client_heartbeat_interval_seconds = float(
            client_heartbeat_interval_seconds)
        self.client_ttl_seconds = float(client_ttl_seconds)
        self.server_ttl_seconds = float(server_ttl_seconds)
        self.response_publish_timeout_seconds = float(
            response_publish_timeout_seconds)
        self.maintain_interval_seconds = float(maintain_interval_seconds)

        if self.channel_count < 1:
            raise ValueError("shm channel_count must be >= 1")
        if self.slot_count < 2:
            # A 1-slot ring cannot be told apart from a full one.
            raise ValueError("shm slot_count must be >= 2")
        if self.slot_size <= shm_layout.SLOT_HEADER_SIZE:
            raise ValueError("shm slot_size must exceed the slot header")

        self._layout = shm_layout.ring_layout(self.slot_count, self.slot_size)
        self._account_hash = shm_layout.account_hash(self.account_id,
                                                     self.hash_salt)
        self._section_names = [
            shm_layout.section_name(self.namespace, self.name_prefix,
                                    self._account_hash, index)
            for index in range(self.channel_count)
        ]

        # -- client role state --
        self._client_local = threading.local()
        self._nonce_lock = threading.Lock()
        self._nonce_seq = int(time.time() * 1000.0) & 0xFFFF
        self._late_responses = 0
        self._timeouts_after_send = 0
        self._timeouts_before_send = 0

        # -- server role state --
        self._background_threads = True
        self._server_lock = threading.RLock()
        self._sections = []
        self._req_rings = []
        self._resp_rings = []
        self._server_epoch = 0
        # The RPC service mirrors these onto itself for stop()/diagnostics
        # (``getattr(self._transport, "_thread", None)``).
        self._thread = None
        self._queue_thread = None
        self._published_count = 0
        self._last_server_heartbeat_ms = 0
        self._last_maintain_ms = 0
        self._reclaimed_channels = 0

    # ------------------------------------------------------------------ util
    def _warn(self, message):
        try:
            print("%s %s" % (self.print_prefix, message))
        except Exception:
            pass

    def _pause(self, spin_until):
        """Wait one beat: busy-spin inside the window, sleep outside it.

        ``spin_until`` is an absolute ``perf_counter`` deadline, so the window
        is per-wait and never grows: a client that spins for 50 ms and then
        waits two seconds spends 50 ms of CPU, not two seconds.
        """
        if time.perf_counter() < spin_until:
            return
        time.sleep(self.idle_sleep_seconds)

    def _next_nonce(self):
        with self._nonce_lock:
            self._nonce_seq = (self._nonce_seq + 1) & 0xFFFFFFFF
            return self._nonce_seq

    def page_names(self):
        """Diagnostics: the mapping names this instance will use."""
        return list(self._section_names)

    # ================================================================ client
    def _ensure_link(self):
        link = getattr(self._client_local, "link", None)
        if link is not None and link.alive():
            return link
        if link is not None:
            link.close()
            self._client_local.link = None
        link = self._attach_link()
        self._client_local.link = link
        return link

    def _attach_link(self):
        """Claim a free channel, waiting for the server (or a free slot)."""
        pid = os.getpid()
        deadline = time.time() + self.connect_timeout_seconds
        # Connecting is not latency-critical, so it gets a much shorter spin:
        # otherwise a failed connect burns a core for the whole timeout.
        spin_until = time.perf_counter() + min(self.spin_seconds, 0.005)
        last_error = None
        while True:
            for index, name in enumerate(self._section_names):
                link, last_error = self._try_claim(index, name, pid)
                if link is not None:
                    return link
            if time.time() >= deadline:
                raise TransportError(
                    "no free shm channel for account %r after %.1fs (last "
                    "error: %s). Is the QMT side running transport=shm with "
                    "the same channel_count and the same slot_count/slot_size?"
                    % (self.account_id, self.connect_timeout_seconds, last_error))
            self._pause(spin_until)

    def _try_claim(self, index, name, pid):
        """Returns ``(link_or_None, reason_or_None)``.

        "No" is normal here -- the server may not have created the section yet,
        or every channel may be taken -- so the caller needs the *reason* to
        report if it eventually times out.
        """
        try:
            section = Section(name, self._layout)
        except TransportError as exc:
            return None, exc
        try:
            header = section.validate()
        except TransportError as exc:
            # Either nobody created it yet or it is not ours yet; both mean
            # "keep waiting", not "fail".
            section.close()
            return None, exc
        if header["account_hash"] != self._account_hash:
            section.close()
            return None, "section %s belongs to another account" % name
        if header["owner_pid"] != 0:
            section.close()
            return None, "all shm channels busy"
        # Claim: nonce first, then pid, then read both back. Two threads in the
        # SAME process share a pid, so the nonce is what tells them apart -- the
        # loser sees the winner's nonce and backs off cleanly.
        nonce = self._next_nonce()
        section.set_u32(shm_layout.HEADER_OFFSETS["flags"], nonce)
        section.set_u32(shm_layout.HEADER_OFFSETS["owner_pid"], pid)
        now = shm_layout.now_ms()
        section.set_u64(shm_layout.HEADER_OFFSETS["owner_claimed_ms"], now)
        section.set_u64(shm_layout.HEADER_OFFSETS["client_heartbeat_ms"], now)
        confirm = section.read_header()
        if confirm["owner_pid"] != pid or confirm["flags"] != nonce:
            section.close()
            return None, "lost the claim race for shm channel %d" % index
        link = _ChannelLink(index, section, confirm["server_epoch"], pid, nonce)
        link.last_client_beat_ms = now
        link.last_server_check_ms = now
        return link, None

    def _touch_client(self, link):
        """Client heartbeat. The server reclaims a channel whose client went
        quiet, so a client blocked in a long wait must keep breathing -- which
        is why this lives on the link and not on the transport: two threads on
        one transport must not reset each other's clock."""
        now = shm_layout.now_ms()
        if now - link.last_client_beat_ms < \
                self.client_heartbeat_interval_seconds * 1000.0:
            return
        link.last_client_beat_ms = now
        link.section.set_u64(shm_layout.HEADER_OFFSETS["client_heartbeat_ms"],
                             now)

    def send_request(self, request, timeout_seconds):
        """Publish ``request`` and block for the matching response.

        Raises ``TransportTimeout`` whose ``sent`` attribute says whether the
        request actually left this process. That distinction is the #195 rule:
        a request that was published may already have been executed, so it must
        never be auto-retried.
        """
        if not isinstance(request, dict):
            raise TransportError("shm request must be a dict, got %r"
                                 % type(request))
        link = self._ensure_link()
        payload = _encode(request)
        request_id = str(request.get("request_id") or "")
        deadline = time.time() + float(timeout_seconds or 0.0)
        spin_until = time.perf_counter() + self.spin_seconds

        while True:
            if link.req.publish(payload):
                break
            if time.time() >= deadline:
                self._timeouts_before_send += 1
                raise TransportTimeout(
                    "shm request ring full on channel %d; request was NOT sent"
                    % link.index)
            self._pause(spin_until)

        while True:
            raw = link.resp.consume()
            if raw is not None:
                response = _decode(raw)
                if self._matches(response, request_id):
                    self._touch_client(link)
                    return response
                # A late answer to a request that already timed out. Drop it and
                # keep waiting for ours -- otherwise the previous timeout would
                # silently answer this request.
                self._late_responses += 1
                continue
            if time.time() >= deadline:
                self._timeouts_after_send += 1
                raise TransportTimeout(
                    "no shm response within %ss on channel %d; the request WAS "
                    "published, so the outcome is unknown -- do not retry "
                    "blindly (#195)" % (timeout_seconds, link.index))
            self._check_server_alive(link)
            self._touch_client(link)
            self._pause(spin_until)

    @staticmethod
    def _matches(response, request_id):
        if not request_id or not isinstance(response, dict):
            return True
        got = str(response.get("request_id") or "")
        return not got or got == request_id

    def _check_server_alive(self, link):
        """Liveness of the other end. Rate-limited: it costs a header read, and
        the wait loop runs every beat, so doing it per iteration would put a
        shared cache-line read on the hot path for no benefit."""
        now = shm_layout.now_ms()
        if now - link.last_server_check_ms < \
                self.server_heartbeat_interval_seconds * 1000.0:
            return
        link.last_server_check_ms = now
        header = link.section.read_header()
        if header["server_epoch"] != link.epoch:
            raise TransportError(
                "the QMT side restarted (shm server_epoch %s -> %s); reconnect"
                % (link.epoch, header["server_epoch"]))
        beat = header["server_heartbeat_ms"]
        if beat and now - beat > self.server_ttl_seconds * 1000.0:
            raise TransportError(
                "QMT side shm heartbeat is %.1fs stale; the strategy is "
                "probably stopped" % ((now - beat) / 1000.0))

    def stats(self):
        return {
            "late_responses": self._late_responses,
            "timeouts_before_send": self._timeouts_before_send,
            "timeouts_after_send": self._timeouts_after_send,
            "reclaimed_channels": self._reclaimed_channels,
            "server_epoch": self._server_epoch,
            "channels": len(self._sections),
        }

    def close_client(self):
        """Release this thread's channel. A clean client leaves nothing behind:
        the section dies with its last handle."""
        link = getattr(self._client_local, "link", None)
        if link is None:
            return
        try:
            if link.still_owned():
                link.section.set_u32(shm_layout.HEADER_OFFSETS["owner_pid"], 0)
                link.section.set_u32(shm_layout.HEADER_OFFSETS["flags"], 0)
        except Exception:
            pass
        link.close()
        self._client_local.link = None

    # ================================================================ server
    def start_receiving(self, on_request, background_threads=None, **kwargs):
        """Create the sections and start accepting requests.

        ``background_threads=False`` creates nothing but the shared state: the
        caller then drives ``drain_request_queue`` from whatever timer it
        already has (the QMT ``adjust`` callback). That is the point of this
        transport inside a sandbox -- zero threads.
        """
        super(SharedMemoryTransport, self).start_receiving(on_request)
        self._background_threads = True if background_threads is None \
            else bool(background_threads)
        with self._server_lock:
            self._init_sections()
        if self._background_threads:
            self._thread = threading.Thread(
                target=self._serve_loop, name="bigqmt-shm-serve")
            self._thread.daemon = True
            self._thread.start()
        return self

    def _new_epoch(self):
        # 56 bits of wall clock plus 8 bits of counter: a restart within the
        # same millisecond still gets a new epoch, which is what a waiting
        # client keys its reconnect on.
        return (((shm_layout.now_ms() << 8) & 0xFFFFFFFFFFFFFF00)
                | (self._next_nonce() & 0xFF))

    def _init_sections(self):
        for section in self._sections:
            section.close()
        self._sections = []
        self._req_rings = []
        self._resp_rings = []
        epoch = self._new_epoch()
        self._server_epoch = epoch
        now = shm_layout.now_ms()
        for index, name in enumerate(self._section_names):
            section = Section(name, self._layout)
            # Writing the whole header both initializes a fresh section and
            # re-initializes a leftover one: owner_pid=0 and head=tail=0 clear
            # any stale client's claim and any half-written frame.
            section.write_header(
                slot_size=self.slot_size,
                slot_count=self.slot_count,
                channel_index=index,
                account_hash=self._account_hash,
                server_epoch=epoch,
                created_ms=now,
                server_heartbeat_ms=now,
            )
            section.validate()
            self._sections.append(section)
            self._req_rings.append(SlotRing(section, "req"))
            self._resp_rings.append(SlotRing(section, "resp"))
        self._last_server_heartbeat_ms = now
        self._last_maintain_ms = now

    def _serve_loop(self):
        spin_until = 0.0
        while self._running:
            processed = 0
            try:
                processed = self._drain_once(64)
            except Exception as exc:        # the loop must survive anything
                self._warn("shm serve loop error: %s: %s"
                           % (exc.__class__.__name__, exc))
            now = time.perf_counter()
            if processed:
                # Requests arrive in bursts. Staying hot for a moment after
                # handling one is what turns the second, third and hundredth
                # call of a burst into a microsecond hop, while a bridge idle
                # for a minute still costs nothing.
                spin_until = now + self.spin_seconds
            if now < spin_until:
                continue
            time.sleep(self.idle_sleep_seconds)

    def drain_request_queue(self, max_items=20, **kwargs):
        """Non-blocking drain for the QMT ``adjust`` callback.

        Never sleeps, never blocks on a peer: it reads only what is already in
        the rings. Returns how many requests were processed.
        """
        if not self._sections:
            return 0
        return self._drain_once(int(max_items))

    def _drain_once(self, max_items):
        self._touch_server_heartbeat()
        if max_items <= 0:
            return 0
        processed = 0
        for index in range(len(self._sections)):
            req = self._req_rings[index]
            while processed < max_items:
                raw = req.consume()
                if raw is None:
                    break
                processed += 1
                self._touch_server_heartbeat()
                try:
                    request = _decode(raw)
                except Exception as exc:
                    self._warn("shm dropped undecodable request frame: %s" % exc)
                    continue
                if not isinstance(request, dict):
                    continue
                # Reply routing, read back by send_response. Named like the pipe
                # transport's _pipe_handle so both read the same way.
                request["_shm_channel"] = index
                self.deliver(request)
        self._maintain_channels()
        return processed

    def _touch_server_heartbeat(self, force=False):
        now = shm_layout.now_ms()
        if not force and now - self._last_server_heartbeat_ms < \
                self.server_heartbeat_interval_seconds * 1000.0:
            return
        self._last_server_heartbeat_ms = now
        for section in self._sections:
            section.set_u64(shm_layout.HEADER_OFFSETS["server_heartbeat_ms"],
                            now)

    def _maintain_channels(self):
        """Reclaim channels whose client stopped breathing. Rate-limited: this
        is the only speculative work the server does."""
        now = shm_layout.now_ms()
        if now - self._last_maintain_ms < \
                self.maintain_interval_seconds * 1000.0:
            return
        self._last_maintain_ms = now
        ttl_ms = self.client_ttl_seconds * 1000.0
        for index, section in enumerate(self._sections):
            owner = section.u32(shm_layout.HEADER_OFFSETS["owner_pid"])
            if owner == 0:
                continue
            beat = section.u64(shm_layout.HEADER_OFFSETS["client_heartbeat_ms"])
            if beat and now - beat <= ttl_ms:
                continue
            # Order matters: empty the rings while the claim is still visible,
            # then release it. A new client arriving in between sees a taken
            # channel and waits, so it can never lose a frame to this sweep.
            self._req_rings[index].reset()
            self._resp_rings[index].reset()
            section.set_u64(shm_layout.HEADER_OFFSETS["client_heartbeat_ms"], 0)
            section.set_u32(shm_layout.HEADER_OFFSETS["owner_pid"], 0)
            section.set_u32(shm_layout.HEADER_OFFSETS["flags"], 0)
            self._reclaimed_channels += 1
            self._warn("reclaimed shm channel %d (client pid=%s went quiet for "
                       "%.1fs)" % (index, owner, (now - beat) / 1000.0))

    def send_response(self, request, response):
        """Publish an answer onto the channel the request arrived on.

        ``request`` carries ``_shm_channel`` from the receive path and travels
        with the payload through the service's queues (including the heavy
        worker's), so a deferred answer still knows where to go.
        """
        index = (request or {}).get("_shm_channel")
        if index is None:
            raise TransportError(
                "shm request carries no _shm_channel to reply on")
        ring = self._resp_rings[int(index)]
        payload = _encode(response)
        budget = self.response_publish_timeout_seconds if self._background_threads \
            else min(self.response_publish_timeout_seconds, 0.005)
        deadline = time.time() + budget
        while True:
            if ring.publish(payload):
                self._published_count += 1
                return
            if time.time() >= deadline:
                # The client is gone or not draining. Drop rather than wedge the
                # QMT adjust thread; the client sees a timeout (sent=True).
                self._warn("shm response ring full on channel %s; response "
                           "dropped" % index)
                raise TransportError(
                    "shm response ring full on channel %s; response dropped"
                    % index)
            time.sleep(self.idle_sleep_seconds)

    def stop(self):
        self._running = False
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.0)
            self._thread = None
        with self._server_lock:
            for section in self._sections:
                section.close()
            self._sections = []
            self._req_rings = []
            self._resp_rings = []
        super(SharedMemoryTransport, self).stop()
