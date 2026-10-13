# coding: utf-8
"""Wire layout of the shared-memory transport (see ``shm_transport``).

Pure constants and ``struct`` math: no ``mmap``, no OS calls, no imports beyond
the standard library. That is deliberate -- this module has to be importable
inside a sandbox whose only remaining primitive is ``mmap``.

Layout
------
::

    +--------------------------------------------------+ 0
    | header (256 bytes reserved, 136 used)            |
    +--------------------------------------------------+ HEADER_SIZE
    | request ring: slot_count * slot_size             |   client -> server
    +--------------------------------------------------+
    | response ring: slot_count * slot_size            |   server -> client
    +--------------------------------------------------+

Each ring is a fixed-size FIFO of slots, each slot a small header followed by
the JSON payload. Only two counters are shared per ring (``head`` written by the
producer, ``tail`` written by the consumer), which keeps both directions
single-producer/single-consumer -- the only concurrency shape that can be
implemented correctly without any cross-process lock.

Why the slot state has no WRITING value
---------------------------------------
The producer writes the length and the payload bytes *before* flipping the slot
state to ``READY``. The consumer gates on ``READY``, so an in-flight write is
invisible by construction and an intermediate state would buy nothing. Fewer
states, fewer bugs. Ordering is guaranteed on x86-64 (TSO: stores are not
reordered with other stores), which is the only architecture Big QMT runs on.
"""

import struct

MAGIC = b"BQSHMRPC"
LAYOUT_VERSION = 1

#: Bytes reserved at the front of the section for the header. The struct below
#: only uses 136, the remainder is headroom for a future layout bump without
#: having to move the rings.
HEADER_SIZE = 256

#: Per-slot header: state(u32) length(u32) seq(u64).
SLOT_HEADER_SIZE = 16
SLOT_FMT = "<IIQ"
SLOT_STRUCT = struct.Struct(SLOT_FMT)

#: Slot states.
SLOT_EMPTY = 0
SLOT_READY = 1

#: Header field order. Keep in lockstep with ``_HEADER_FMT``.
HEADER_FIELDS = (
    "magic",
    "layout_version",
    "header_size",
    "slot_size",
    "slot_count",
    "channel_index",
    "account_hash",
    "server_epoch",
    "created_ms",
    "server_heartbeat_ms",
    "client_heartbeat_ms",
    "owner_pid",
    "flags",
    "owner_claimed_ms",
    "req_head",
    "req_tail",
    "resp_head",
    "resp_tail",
    "req_overflow",
    "resp_overflow",
    "last_error",
    "reserved",
)

_HEADER_FMT = "<8sIIIIIIQQQQIIQQQQQQQII"
HEADER_STRUCT = struct.Struct(_HEADER_FMT)


def _iter_format_codes(fmt):
    """Yield ``(count, code)`` for a struct format string, expanding repeats."""
    body = fmt[1:]                       # drop the byte-order character
    index = 0
    while index < len(body):
        start = index
        while index < len(body) and body[index].isdigit():
            index += 1
        count = int(body[start:index]) if index > start else 1
        code = body[index]
        index += 1
        yield count, code


#: byte offset of every header field. Derived, never hand-maintained: a
#: hand-written offset table drifts the moment somebody adds a field, and the
#: failure mode is silent corruption of a neighbouring counter.
HEADER_OFFSETS = {}
_cursor = 0
_header_codes = list(_iter_format_codes(_HEADER_FMT))
if len(_header_codes) != len(HEADER_FIELDS):
    # zip() would silently truncate and every offset past the gap would be
    # wrong -- the worst possible failure, so it is an import-time assert.
    raise AssertionError(
        "header format has %d codes but %d field names"
        % (len(_header_codes), len(HEADER_FIELDS)))
for (_count, _code), _name in zip(_header_codes, HEADER_FIELDS):
    HEADER_OFFSETS[_name] = _cursor
    _cursor += struct.calcsize("<%d%s" % (_count, _code))
del _count, _code, _name, _cursor, _header_codes

assert HEADER_STRUCT.size == 136, HEADER_STRUCT.size
assert HEADER_STRUCT.size <= HEADER_SIZE
assert HEADER_OFFSETS["reserved"] + 4 <= HEADER_SIZE

#: Defaults. slot_size has to hold the largest single frame the caller sends --
#: the pipe transport sized its buffer at 1 MiB for whole-market quotes, so a
#: deployment that pushes those must raise this on BOTH sides.
DEFAULT_SLOT_SIZE = 64 * 1024
DEFAULT_SLOT_COUNT = 64
DEFAULT_CHANNEL_COUNT = 4

#: A named section without a namespace prefix lands in whichever namespace the
#: OS picks; being explicit is what makes QMT (an interactive session) and the
#: external client land on the same object. ``Global\`` is available for the
#: service/session-crossing case.
DEFAULT_NAMESPACE = "Local"
DEFAULT_NAME_PREFIX = "bigqmt_shm"


def header_index(field):
    """Position of ``field`` in the unpacked header tuple."""
    return HEADER_FIELDS.index(field)


def header_to_dict(values):
    return dict(zip(HEADER_FIELDS, values))


def dict_to_header(mapping):
    """Build a header tuple from a mapping. Unmentioned numeric fields are 0.

    Defaulting to 0 rather than requiring every caller to name every counter is
    what makes "initialize a fresh section" a one-liner; a missing *layout*
    field is still caught by ``Section.validate`` on the peer.
    """
    values = {name: 0 for name in HEADER_FIELDS}
    values.update({
        "magic": MAGIC,
        "layout_version": LAYOUT_VERSION,
        "header_size": HEADER_SIZE,
    })
    values.update(mapping or {})
    return tuple(values[name] for name in HEADER_FIELDS)


def ring_layout(slot_count, slot_size):
    """Byte offsets of both rings. Same numbers on both sides of the wire."""
    slot_count = int(slot_count)
    slot_size = int(slot_size)
    req_offset = HEADER_SIZE
    resp_offset = req_offset + slot_count * slot_size
    total = resp_offset + slot_count * slot_size
    return {
        "slot_count": slot_count,
        "slot_size": slot_size,
        "req_offset": req_offset,
        "resp_offset": resp_offset,
        "total_size": total,
    }


def max_payload_size(slot_size):
    return int(slot_size) - SLOT_HEADER_SIZE


def section_name(namespace, prefix, account_hash, channel_index):
    """Full mapping name.

    The account is part of the name, which is the one thing this transport has
    that pipe and mysql do not: a per-account endpoint the client can derive
    without being told. Two deployments -- one live, one simulated -- therefore
    never share a wire (#144 in the parent bridge was exactly that: two
    strategies silently sharing one log file).
    """
    ns = str(namespace or DEFAULT_NAMESPACE).rstrip("\\")
    body = "%s_%08x_%d" % (str(prefix or DEFAULT_NAME_PREFIX),
                           int(account_hash), int(channel_index))
    return "%s\\%s" % (ns, body)


def account_hash(account_id, salt=""):
    """Stable 32-bit hash of the account id.

    NOT ``hash()``: that is randomised per process (PYTHONHASHSEED), so the two
    sides would derive different section names and never meet.
    """
    import zlib

    blob = ("%s|%s" % (salt or "", account_id or "")).encode("utf-8")
    return zlib.crc32(blob) & 0xFFFFFFFF


def now_ms():
    """Wall-clock milliseconds.

    Wall clock, not monotonic: monotonic lacks a shared epoch across processes,
    and the only things compared here are timestamps *written by the other
    process* (heartbeats). A few seconds of clock drift is far below the
    staleness thresholds, and both sides are the same machine by construction.
    """
    import time

    return int(time.time() * 1000.0)
