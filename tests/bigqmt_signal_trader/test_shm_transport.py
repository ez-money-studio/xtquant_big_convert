# coding: utf-8
"""Shared-memory transport: layout, ring, round trip, and its sandbox premise.

The premise is the reason this transport exists, so it is asserted rather than
documented: the runtime must never import ``socket``/``ctypes``/``tempfile``,
never call ``open()``, never map a file-backed section, and never use
``ACCESS_READ`` (which on Windows silently *creates* a read-only section and
poisons the name for a later read-write attach).
"""

import ast
import os
import sys
import struct
import threading
import time
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if os.path.join(_ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(_ROOT, "src"))

from bigqmt_signal_trader.transports import shm_layout
from bigqmt_signal_trader.transports.base import TransportError, TransportTimeout
from bigqmt_signal_trader.transports.factory import (
    build_transport,
    transport_supports_drain,
)
from bigqmt_signal_trader.transports.shm_ring import Section, SlotRing
from bigqmt_signal_trader.transports.shm_transport import SharedMemoryTransport

PACKAGE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src", "bigqmt_signal_trader", "transports")
SHM_MODULES = ("shm_layout.py", "shm_ring.py", "shm_transport.py")


def echo_handler(request):
    return {
        "schema_version": 1,
        "request_id": request.get("request_id"),
        "ok": True,
        "data": {"method": request.get("method"), "params": request.get("params")},
    }


def release_account(account, channel_count=4, slot_count=None, slot_size=None):
    """Attach and immediately release, so a section left behind by a failed
    test cannot be inherited by the next one. Sections die with their last
    handle, so this is a no-op when nothing is there."""
    layout = shm_layout.ring_layout(
        slot_count or shm_layout.DEFAULT_SLOT_COUNT,
        slot_size or shm_layout.DEFAULT_SLOT_SIZE)
    released = []
    for index in range(channel_count):
        name = shm_layout.section_name(
            shm_layout.DEFAULT_NAMESPACE, shm_layout.DEFAULT_NAME_PREFIX,
            shm_layout.account_hash(account, ""), index)
        try:
            section = Section(name, layout)
        except Exception:
            continue
        section.close()
        released.append(name)
    return released


class ShmLayoutTest(unittest.TestCase):

    def test_header_fits_its_reservation(self):
        self.assertEqual(shm_layout.HEADER_STRUCT.size, 136)
        self.assertLessEqual(shm_layout.HEADER_STRUCT.size,
                             shm_layout.HEADER_SIZE)
        self.assertEqual(shm_layout.HEADER_OFFSETS["reserved"],
                         shm_layout.HEADER_STRUCT.size - 4)

    def test_offsets_match_the_struct_positions(self):
        # Pack a distinguishable value into every field, then read each one back
        # through its runtime offset. This is the test that catches a field
        # added to HEADER_FIELDS but not to the format string.
        marker = {}
        for index, name in enumerate(shm_layout.HEADER_FIELDS):
            marker[name] = shm_layout.MAGIC if name == "magic" else index + 1
        raw = shm_layout.HEADER_STRUCT.pack(
            *shm_layout.dict_to_header(marker))
        for name in shm_layout.HEADER_FIELDS:
            if name == "magic":
                continue
            offset = shm_layout.HEADER_OFFSETS[name]
            code = "<I" if name in _THIRTY_TWO_BIT else "<Q"
            self.assertEqual(struct.unpack_from(code, raw, offset)[0],
                             marker[name], name)

    def test_rings_are_back_to_back_after_the_header(self):
        layout = shm_layout.ring_layout(slot_count=8, slot_size=1024)
        self.assertEqual(layout["req_offset"], shm_layout.HEADER_SIZE)
        self.assertEqual(layout["resp_offset"],
                         shm_layout.HEADER_SIZE + 8 * 1024)
        self.assertEqual(layout["total_size"],
                         shm_layout.HEADER_SIZE + 2 * 8 * 1024)
        self.assertEqual(shm_layout.max_payload_size(1024), 1024 - 16)

    def test_section_name_separates_accounts_and_channels(self):
        name = shm_layout.section_name("Local", "bigqmt_shm", 0xDEADBEEF, 3)
        self.assertEqual(name, "Local\\bigqmt_shm_deadbeef_3")
        self.assertNotEqual(shm_layout.section_name("Local", "p", 1, 0),
                            shm_layout.section_name("Local", "p", 1, 1))
        self.assertNotEqual(shm_layout.section_name("Local", "p", 1, 0),
                            shm_layout.section_name("Local", "p", 2, 0))
        self.assertNotEqual(shm_layout.section_name("Local", "p", 1, 0),
                            shm_layout.section_name("Global", "p", 1, 0))

    def test_account_hash_is_stable(self):
        # hash() is randomised per process, so its value must never appear here:
        # the two sides would derive different names and never meet.
        first = shm_layout.account_hash("123456", "salt")
        self.assertEqual(first, shm_layout.account_hash("123456", "salt"))
        self.assertNotEqual(first, shm_layout.account_hash("123457", "salt"))
        self.assertNotEqual(first, shm_layout.account_hash("123456", "other"))
        self.assertEqual(first, first & 0xFFFFFFFF)


_THIRTY_TWO_BIT = frozenset((
    "layout_version", "header_size", "slot_size", "slot_count",
    "channel_index", "account_hash", "owner_pid", "flags", "last_error",
    "reserved",
))


class RingTest(unittest.TestCase):

    def make_ring(self, slot_count=4, slot_size=256, direction="req",
                  tag="ring"):
        name = shm_layout.section_name("Local", "bigqmt_shm_test_%s" % tag,
                                       0xA1, 0)
        layout = shm_layout.ring_layout(slot_count, slot_size)
        section = Section(name, layout)
        section.write_header(slot_size=slot_size, slot_count=slot_count,
                             account_hash=0xA1, server_epoch=1)
        return section, SlotRing(section, direction)

    def test_publish_then_consume_preserves_order(self):
        section, ring = self.make_ring()
        try:
            for index in range(4):
                self.assertTrue(ring.publish(b"frame-%d" % index))
            self.assertEqual(ring.pending(), 4)
            for index in range(4):
                self.assertEqual(ring.consume(), b"frame-%d" % index)
            self.assertIsNone(ring.consume())
        finally:
            section.close()

    def test_wrap_around_reuses_slots(self):
        section, ring = self.make_ring(slot_count=4, tag="wrap")
        try:
            for index in range(12):
                self.assertTrue(ring.publish(b"x%d" % index))
                self.assertEqual(ring.consume(), b"x%d" % index)
            self.assertEqual(ring.pending(), 0)
        finally:
            section.close()

    def test_full_ring_refuses_and_counts_the_overflow(self):
        section, ring = self.make_ring(slot_count=2, tag="full")
        try:
            self.assertTrue(ring.publish(b"one"))
            self.assertTrue(ring.publish(b"two"))
            self.assertFalse(ring.publish(b"three"))
            self.assertEqual(
                section.u64(shm_layout.HEADER_OFFSETS["req_overflow"]), 1)
        finally:
            section.close()

    def test_oversized_frame_is_refused_loudly(self):
        section, ring = self.make_ring(slot_count=2, slot_size=64, tag="big")
        try:
            with self.assertRaises(TransportError):
                ring.publish(b"z" * (shm_layout.max_payload_size(64) + 1))
            # Exactly at the limit still fits -- the boundary is not off by one.
            limit = shm_layout.max_payload_size(64)
            self.assertTrue(ring.publish(b"z" * limit))
            self.assertEqual(ring.consume(), b"z" * limit)
        finally:
            section.close()

    def test_directions_are_independent(self):
        section, req = self.make_ring(slot_count=2, direction="req", tag="dir")
        resp = SlotRing(section, "resp")
        try:
            req.publish(b"request")
            self.assertEqual(resp.pending(), 0)
            self.assertIsNone(resp.consume())
            self.assertEqual(req.pending(), 1)
        finally:
            section.close()

    def test_validate_rejects_an_uninitialised_section(self):
        name = shm_layout.section_name("Local", "bigqmt_shm_test_blank", 0xB2, 0)
        section = Section(name, shm_layout.ring_layout(4, 256))
        try:
            with self.assertRaises(TransportError):
                section.validate()
        finally:
            section.close()

    def test_validate_rejects_a_layout_mismatch(self):
        section, _ring = self.make_ring(tag="mismatch")
        try:
            # A peer configured for a bigger section cannot even map it: the
            # view would run past the end of the object. Either way it must fail
            # loudly rather than be handed shifted offsets.
            with self.assertRaises(Exception):
                Section(section.name, shm_layout.ring_layout(8, 256))
        finally:
            section.close()


class RoundTripTest(unittest.TestCase):

    def setUp(self):
        release_account("shm-roundtrip")
        self.server = SharedMemoryTransport(account_id="shm-roundtrip")
        self.server.start_receiving(echo_handler, background_threads=True)
        self.client = SharedMemoryTransport(account_id="shm-roundtrip")

    def tearDown(self):
        self.client.close_client()
        self.server.stop()

    def test_a_call_returns_the_handler_response(self):
        response = self.client.send_request(
            {"request_id": "r1", "method": "ping"}, 5.0)
        self.assertTrue(response["ok"])
        self.assertEqual(response["data"]["method"], "ping")

    def test_repeated_calls_keep_working(self):
        for index in range(25):
            response = self.client.send_request(
                {"request_id": "r%d" % index, "method": "echo",
                 "params": {"n": index}}, 5.0)
            self.assertEqual(response["data"]["params"]["n"], index)

    def test_large_payload_survives_the_round_trip(self):
        blob = "x" * 40000
        response = self.client.send_request(
            {"request_id": "big", "method": "echo", "params": {"blob": blob}},
            5.0)
        self.assertEqual(response["data"]["params"]["blob"], blob)

    def test_handler_exception_becomes_an_error_envelope(self):
        def boom(_request):
            raise ValueError("handler said no")

        release_account("shm-boom")
        server = SharedMemoryTransport(account_id="shm-boom")
        server.start_receiving(boom, background_threads=True)
        client = SharedMemoryTransport(account_id="shm-boom")
        try:
            response = client.send_request({"request_id": "b1"}, 5.0)
            self.assertFalse(response["ok"])
            self.assertIn("ValueError", response["error"])
        finally:
            client.close_client()
            server.stop()

    def test_no_pathological_stall_per_call(self):
        """A loose guard, not a benchmark.

        The measured cross-process p50 is ~0.06 ms. The failure this guards
        against is not "a bit slower" but "both ends fell back to the ~15.6 ms
        Windows scheduler tick and every call now costs 31 ms", so the bound
        sits far above the real number and far below that failure.
        """
        started = time.time()
        for index in range(20):
            self.client.send_request({"request_id": "s%d" % index}, 5.0)
        self.assertLess(time.time() - started, 1.0,
                        "20 calls took over a second -- polling is falling back "
                        "to the OS timer tick")


class DrainModeTest(unittest.TestCase):
    """No thread at all: the host drives the pump, as QMT's adjust does."""

    def setUp(self):
        release_account("shm-drain")
        self.seen = []

        def handler(request):
            self.seen.append(request.get("method"))
            return {"ok": True, "request_id": request.get("request_id")}

        self.server = SharedMemoryTransport(account_id="shm-drain")
        self.server.start_receiving(handler, background_threads=False)
        self.client = SharedMemoryTransport(account_id="shm-drain")

    def tearDown(self):
        self.client.close_client()
        self.server.stop()

    def test_nothing_is_answered_until_the_host_pumps(self):
        results = {}

        def caller():
            try:
                results["response"] = self.client.send_request(
                    {"request_id": "d1", "method": "tick"}, 5.0)
            except Exception as exc:        # pragma: no cover - diagnostic
                results["error"] = exc

        worker = threading.Thread(target=caller)
        worker.daemon = True
        worker.start()
        time.sleep(0.15)
        self.assertEqual(self.seen, [], "drain mode must not answer on its own")
        deadline = time.time() + 5.0
        while not self.seen and time.time() < deadline:
            self.server.drain_request_queue(max_items=10)
            time.sleep(0.002)
        worker.join(timeout=5.0)
        self.assertNotIn("error", results)
        self.assertTrue(results["response"]["ok"])

    def test_drain_is_non_blocking_when_idle(self):
        started = time.time()
        for _ in range(20):
            self.assertEqual(self.server.drain_request_queue(max_items=10), 0)
        self.assertLess(time.time() - started, 1.0)


class TimeoutSemanticsTest(unittest.TestCase):
    """``sent`` is the difference between "safe to retry" and the #195 rule."""

    def test_timeout_after_publish_says_so(self):
        release_account("shm-slow")

        def slow(_request):
            time.sleep(0.6)
            return {"ok": True}

        server = SharedMemoryTransport(account_id="shm-slow")
        server.start_receiving(slow, background_threads=True)
        client = SharedMemoryTransport(account_id="shm-slow")
        try:
            with self.assertRaises(TransportTimeout) as caught:
                client.send_request({"request_id": "t1"}, 0.15)
            self.assertIn("unknown", str(caught.exception))
            self.assertEqual(client.stats()["timeouts_after_send"], 1)
        finally:
            client.close_client()
            server.stop()

    def test_timeout_before_publish_says_not_sent(self):
        release_account("shm-backpressure", slot_count=2)
        # A server that exists (so a channel can be claimed) but never drains.
        server = SharedMemoryTransport(account_id="shm-backpressure",
                                       slot_count=2)
        server.start_receiving(echo_handler, background_threads=False)
        client = SharedMemoryTransport(account_id="shm-backpressure",
                                       slot_count=2)
        try:
            link = client._ensure_link()
            self.assertTrue(link.req.publish(b"{}"))
            self.assertTrue(link.req.publish(b"{}"))
            with self.assertRaises(TransportTimeout) as caught:
                client.send_request({"request_id": "b1"}, 0.2)
            self.assertIn("NOT sent", str(caught.exception))
            self.assertEqual(client.stats()["timeouts_before_send"], 1)
        finally:
            client.close_client()
            server.stop()

    def test_late_response_is_not_given_to_the_next_request(self):
        release_account("shm-late", slot_count=8)

        def slow_then_fast(request):
            if request.get("method") == "slow":
                time.sleep(0.5)
                return {"ok": True, "request_id": request.get("request_id"),
                        "data": "slow-answer"}
            return {"ok": True, "request_id": request.get("request_id"),
                    "data": "fast-answer"}

        server = SharedMemoryTransport(account_id="shm-late", slot_count=8)
        server.start_receiving(slow_then_fast, background_threads=True)
        client = SharedMemoryTransport(account_id="shm-late", slot_count=8)
        try:
            with self.assertRaises(TransportTimeout):
                client.send_request({"request_id": "l1", "method": "slow"}, 0.15)
            time.sleep(0.6)      # let the abandoned answer land in the ring
            response = client.send_request(
                {"request_id": "l2", "method": "fast"}, 2.0)
            self.assertEqual(response["data"], "fast-answer")
            self.assertGreaterEqual(client.stats()["late_responses"], 1)
        finally:
            client.close_client()
            server.stop()


class ChannelTopologyTest(unittest.TestCase):

    def test_threads_get_their_own_channels(self):
        release_account("shm-threaded")
        server = SharedMemoryTransport(account_id="shm-threaded")
        server.start_receiving(echo_handler, background_threads=True)
        client = SharedMemoryTransport(account_id="shm-threaded")
        indices = []
        lock = threading.Lock()

        def worker():
            try:
                link = client._ensure_link()
            except Exception as exc:        # pragma: no cover - diagnostic
                with lock:
                    indices.append("error: %s" % exc)
                return
            with lock:
                indices.append(link.index)

        try:
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10.0)
            self.assertEqual(len(indices), 2)
            self.assertNotIsInstance(indices[0], str, indices)
            self.assertNotEqual(indices[0], indices[1],
                                "two threads must never share one SPSC ring")
        finally:
            client.close_client()
            server.stop()

    def test_running_out_of_channels_fails_loudly(self):
        release_account("shm-single", channel_count=1)
        server = SharedMemoryTransport(account_id="shm-single", channel_count=1)
        server.start_receiving(echo_handler, background_threads=True)
        first = SharedMemoryTransport(account_id="shm-single", channel_count=1)
        second = SharedMemoryTransport(account_id="shm-single", channel_count=1,
                                       connect_timeout_seconds=0.3)
        try:
            self.assertTrue(first.send_request({"request_id": "f1"}, 2.0)["ok"])
            with self.assertRaises(TransportError) as caught:
                second.send_request({"request_id": "s1"}, 2.0)
            self.assertIn("no free shm channel", str(caught.exception))
        finally:
            first.close_client()
            second.close_client()
            server.stop()

    def test_dead_client_channel_is_reclaimed(self):
        release_account("shm-reclaim")
        server = SharedMemoryTransport(account_id="shm-reclaim",
                                       client_ttl_seconds=0.2,
                                       maintain_interval_seconds=0.05)
        server.start_receiving(echo_handler, background_threads=False)
        section = Section(server.page_names()[0], server._layout)
        try:
            # A client that claimed the channel and then died: owner set,
            # heartbeat long past the TTL.
            section.set_u32(shm_layout.HEADER_OFFSETS["owner_pid"], 999999)
            section.set_u32(shm_layout.HEADER_OFFSETS["flags"], 7)
            section.set_u64(shm_layout.HEADER_OFFSETS["client_heartbeat_ms"],
                            shm_layout.now_ms() - 60000)
            deadline = time.time() + 3.0
            while time.time() < deadline:
                server.drain_request_queue(max_items=10)
                if section.u32(shm_layout.HEADER_OFFSETS["owner_pid"]) == 0:
                    break
                time.sleep(0.05)
            self.assertEqual(
                section.u32(shm_layout.HEADER_OFFSETS["owner_pid"]), 0)
            self.assertGreaterEqual(server.stats()["reclaimed_channels"], 1)
        finally:
            section.close()
            server.stop()

    def test_live_client_is_not_reclaimed(self):
        release_account("shm-lively")
        server = SharedMemoryTransport(account_id="shm-lively",
                                       client_ttl_seconds=5.0,
                                       maintain_interval_seconds=0.01)
        server.start_receiving(echo_handler, background_threads=True)
        client = SharedMemoryTransport(account_id="shm-lively")
        try:
            client.send_request({"request_id": "k1"}, 2.0)   # claim + beat
            time.sleep(0.1)
            self.assertEqual(server.stats()["reclaimed_channels"], 0)
            self.assertTrue(client.send_request({"request_id": "k2"}, 2.0)["ok"])
        finally:
            client.close_client()
            server.stop()

    def test_client_reconnects_after_a_server_restart(self):
        release_account("shm-restart")
        server = SharedMemoryTransport(account_id="shm-restart")
        server.start_receiving(echo_handler, background_threads=True)
        client = SharedMemoryTransport(account_id="shm-restart")
        try:
            self.assertTrue(client.send_request({"request_id": "p1"}, 2.0)["ok"])
            old_epoch = server.stats()["server_epoch"]
            server.stop()
            server = SharedMemoryTransport(account_id="shm-restart")
            server.start_receiving(echo_handler, background_threads=True)
            self.assertNotEqual(server.stats()["server_epoch"], old_epoch)
            # A stale link must be detected and replaced, not reused: the new
            # server already reset the rings underneath it.
            self.assertTrue(client.send_request({"request_id": "p2"}, 2.0)["ok"])
        finally:
            client.close_client()
            server.stop()


class FactoryIntegrationTest(unittest.TestCase):

    def test_factory_returns_the_shared_memory_transport(self):
        transport = build_transport("shm", {"shm": {"channel_count": 2}},
                                    account_id="factory-shm")
        self.assertIsInstance(transport, SharedMemoryTransport)
        self.assertEqual(transport.channel_count, 2)

    def test_transport_reports_that_it_can_drain(self):
        # The whole point inside a sandbox: the adjust callback pumps it and no
        # thread is created. A false answer here silently forces the receiver
        # thread back on, which is what happened to pipe (#343).
        self.assertTrue(transport_supports_drain("shm"))

    def test_the_two_sandbox_transport_copies_agree(self):
        """The same tuple exists twice on purpose (see adapter_factory): the
        package cannot import the top-level runtime module without reversing a
        dependency the sandboxed loader is sensitive to. Two copies of one list
        drifting apart is this repository's most expensive recurring bug, so
        they are pinned equal here instead of trusted."""
        import bigqmt_signal_trader_redis_rpc_runtime as runtime
        from bigqmt_signal_trader import adapter_factory

        self.assertEqual(tuple(adapter_factory.SANDBOX_TRANSPORTS),
                         tuple(runtime.SANDBOX_TRANSPORTS))
        self.assertIn("shm", runtime.SANDBOX_TRANSPORTS)

    def test_native_xtdata_default_is_off_for_both_sandbox_transports(self):
        # The SDK dials localhost:58610. On the terminal these transports exist
        # for, one such call is the process dying.
        import bigqmt_signal_trader_redis_rpc_runtime as runtime

        for name in runtime.SANDBOX_TRANSPORTS:
            self.assertFalse(runtime._native_xtdata_default(name), name)
        for name in ("redis", "zmq", "mysql", ""):
            self.assertTrue(runtime._native_xtdata_default(name), name)


class SandboxInvariantTest(unittest.TestCase):
    """The premise of the transport, asserted instead of documented."""

    #: Anything that opens a wire, a file, a registry key or a subprocess. Note
    #: ``ctypes`` too: this implementation needs no Win32 call at all, and
    #: losing that property would be easy to miss in review.
    BANNED_IMPORTS = frozenset((
        "socket", "socketserver", "ssl", "select", "selectors", "asyncio",
        "http", "urllib", "ftplib", "smtplib", "poplib", "imaplib",
        "telnetlib", "xmlrpc", "webbrowser",
        "ctypes", "subprocess", "multiprocessing", "winreg",
        "tempfile", "pathlib", "shutil", "glob", "fileinput",
        "sqlite3", "dbm", "shelve", "pickle", "marshal",
    ))
    BANNED_CALLS = frozenset((
        "open", "io.open", "codecs.open",
        "os.open", "os.remove", "os.unlink", "os.mkdir", "os.makedirs",
        "os.rmdir", "os.rename", "os.replace", "os.startfile", "os.system",
        "os.popen", "os.fdopen", "os.scandir", "os.walk", "os.listdir",
    ))

    def sources(self):
        for name in SHM_MODULES:
            path = os.path.join(PACKAGE_DIR, name)
            with open(path, "r", encoding="utf-8") as handle:
                yield name, path, handle.read()

    def test_the_scan_is_not_vacuous(self):
        names = [name for name, _path, _src in self.sources()]
        self.assertEqual(sorted(names), sorted(SHM_MODULES))
        self.assertIn("mmap", dict((n, s) for n, _p, s in self.sources())
                      ["shm_ring.py"])

    def test_no_sandbox_hostile_imports(self):
        offenders = []
        for name, path, source in self.sources():
            for node in ast.walk(ast.parse(source, filename=path)):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] in self.BANNED_IMPORTS:
                            offenders.append("%s:%d imports %s"
                                             % (name, node.lineno, alias.name))
                elif isinstance(node, ast.ImportFrom):
                    if (node.module or "").split(".")[0] in self.BANNED_IMPORTS:
                        offenders.append("%s:%d from %s import ..."
                                         % (name, node.lineno, node.module))
        self.assertEqual(offenders, [], "sandbox-hostile imports: %s" % offenders)

    def test_no_file_touching_calls(self):
        offenders = []
        for name, path, source in self.sources():
            for node in ast.walk(ast.parse(source, filename=path)):
                if isinstance(node, ast.Call) and _dotted(node.func) in \
                        self.BANNED_CALLS:
                    offenders.append("%s:%d calls %s()"
                                     % (name, node.lineno, _dotted(node.func)))
        self.assertEqual(offenders, [], "file-touching calls: %s" % offenders)

    def test_mmap_is_only_ever_used_file_free(self):
        seen = 0
        offenders = []
        for name, path, source in self.sources():
            for node in ast.walk(ast.parse(source, filename=path)):
                if isinstance(node, ast.Call) and _dotted(node.func) == "mmap.mmap":
                    seen += 1
                    first = node.args[0] if node.args else None
                    if not (isinstance(first, ast.UnaryOp)
                            and isinstance(first.op, ast.USub)
                            and getattr(first.operand, "value", None) == 1):
                        offenders.append("%s:%d mmap.mmap(fileno=%r, ...)"
                                         % (name, node.lineno,
                                            ast.dump(first) if first else None))
        self.assertGreater(seen, 0, "no mmap.mmap call found -- scan is stale")
        self.assertEqual(offenders, [], "file-backed mappings: %s" % offenders)

    def test_access_read_is_never_used(self):
        """On Windows, ACCESS_READ on a *missing* name does not fail: it creates
        a read-only section and the name is poisoned for any later read-write
        attach (WinError 87) until that handle closes. Both ends therefore
        always attach read-write and answer "is it up?" by validating the
        header."""
        offenders = []
        for name, path, source in self.sources():
            for node in ast.walk(ast.parse(source, filename=path)):
                # AST, not a substring scan: the trap is documented in prose in
                # shm_ring.py and prose must not fail the build.
                if isinstance(node, ast.Attribute) and node.attr == "ACCESS_READ":
                    offenders.append("%s:%d" % (name, node.lineno))
        self.assertEqual(offenders, [], "ACCESS_READ would poison the name: %s"
                         % offenders)

    def test_every_module_stays_python_36_parseable(self):
        # The QMT side runs 3.6. feature_version rejects walrus, positional-only
        # params and match statements; the two extra checks catch what it does
        # not (PEP 563 is a SyntaxError on 3.6, dataclasses an ImportError).
        offenders = []
        for name, path, source in self.sources():
            try:
                ast.parse(source, filename=path, feature_version=(3, 6))
            except SyntaxError as exc:
                offenders.append("%s: %s" % (name, exc))
            if "from __future__ import annotations" in source:
                offenders.append("%s: PEP 563 future import" % name)
            if "import dataclasses" in source:
                offenders.append("%s: dataclasses is 3.7+" % name)
        self.assertEqual(offenders, [], offenders)


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


if __name__ == "__main__":
    unittest.main()
