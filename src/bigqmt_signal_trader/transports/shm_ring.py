# coding: utf-8
"""The shared-memory section itself: mmap attach plus the two slot rings.

Everything here is one ``mmap`` object and ``struct.pack_into`` /
``unpack_from`` against byte offsets. No ctypes, no events, no mutexes, no
temporary files, no sockets -- see
``tests/bigqmt_signal_trader/test_shm_transport.py``, which fails the build if
that ever stops being true.

The one ordering rule that makes this correct without locks
----------------------------------------------------------
The producer must write the payload *before* it flips the slot state to READY,
and the consumer must observe READY *before* it reads the payload. On x86-64
(the only architecture Big QMT ships) stores are not reordered with other
stores, so "write bytes, then write state" is a release store and "read state,
then read bytes" is an acquire load. CPython adds no reordering of its own.
"""

import mmap
import struct

from . import shm_layout
from .base import TransportError

_U32 = struct.Struct("<I")
_U64 = struct.Struct("<Q")
_SLOT = struct.Struct(shm_layout.SLOT_FMT)

#: header fields stored as 64-bit. Kept explicit so a format change cannot
#: silently start truncating a counter to 32 bits.
_U64_FIELDS = frozenset((
    "server_epoch", "created_ms", "server_heartbeat_ms", "client_heartbeat_ms",
    "owner_claimed_ms", "req_head", "req_tail", "resp_head", "resp_tail",
    "req_overflow", "resp_overflow",
))


def _is_u64(name):
    return name in _U64_FIELDS


class Section(object):
    """A named shared-memory section, attached read-write.

    ``mmap.mmap(-1, size, tagname=name)`` maps a file-free named section: the
    ``-1`` fileno is what keeps this off the filesystem entirely, and the name
    is what lets a second process attach to the *same physical pages*. If the
    name already exists the OS hands back the existing object, so the client
    does not need to know who created it -- it attaches and reads the header.

    ACCESS_READ is never used, and that is not a style choice. On Windows,
    ``mmap(-1, size, tagname=NAME, access=ACCESS_READ)`` on a name that does
    **not** exist does not fail: it *creates* a read-only section and succeeds.
    The name is then poisoned -- any later read-write attach fails with
    ``WinError 87`` until that read-only handle closes (verified on the dev
    machine). So "is the server up yet?" is answered by validating the header,
    never by probing with a read-only attach.
    """

    def __init__(self, name, layout):
        self.name = str(name)
        self.layout = dict(layout)
        size = int(layout["total_size"])
        try:
            self._mm = mmap.mmap(
                -1, size, tagname=self.name, access=mmap.ACCESS_WRITE)
        except (OSError, ValueError) as exc:
            raise TransportError(
                "cannot attach shm section %r (%d bytes): %s. A leftover "
                "section with a different size is the usual cause -- both ends "
                "must agree on slot_count/slot_size." % (self.name, size, exc))
        if self._mm.size() < size:
            self.close()
            raise TransportError(
                "shm section %r is %d bytes on the wire but %d were requested"
                % (self.name, self._mm.size(), size))

    @property
    def mm(self):
        return self._mm

    def u32(self, offset):
        return _U32.unpack_from(self._mm, offset)[0]

    def u64(self, offset):
        return _U64.unpack_from(self._mm, offset)[0]

    def set_u32(self, offset, value):
        _U32.pack_into(self._mm, offset, int(value) & 0xFFFFFFFF)

    def set_u64(self, offset, value):
        _U64.pack_into(self._mm, offset, int(value) & 0xFFFFFFFFFFFFFFFF)

    def read_header(self):
        raw = self._mm[: shm_layout.HEADER_STRUCT.size]
        return shm_layout.header_to_dict(shm_layout.HEADER_STRUCT.unpack(raw))

    def write_header(self, **overrides):
        fields = shm_layout.dict_to_header(overrides)
        shm_layout.HEADER_STRUCT.pack_into(self._mm, 0, *fields)

    def field(self, name):
        offset = shm_layout.HEADER_OFFSETS[name]
        return self.u64(offset) if _is_u64(name) else self.u32(offset)

    def set_field(self, name, value):
        offset = shm_layout.HEADER_OFFSETS[name]
        if _is_u64(name):
            self.set_u64(offset, value)
        else:
            self.set_u32(offset, value)

    def validate(self):
        """Header sanity. Returns the header dict; raises if it is not ours."""
        header = self.read_header()
        if header["magic"] != shm_layout.MAGIC:
            raise TransportError(
                "shm section %r is not a %s section (magic=%r)"
                % (self.name, shm_layout.MAGIC.decode("ascii"), header["magic"]))
        if header["layout_version"] != shm_layout.LAYOUT_VERSION:
            raise TransportError(
                "shm section %r has layout_version=%s, this build speaks %s"
                % (self.name, header["layout_version"],
                   shm_layout.LAYOUT_VERSION))
        if header["slot_size"] != self.layout["slot_size"] or \
                header["slot_count"] != self.layout["slot_count"]:
            raise TransportError(
                "shm section %r layout mismatch: wire slot_size=%s slot_count=%s,"
                " requested %s/%s"
                % (self.name, header["slot_size"], header["slot_count"],
                   self.layout["slot_size"], self.layout["slot_count"]))
        return header

    def close(self):
        try:
            self._mm.close()
        except Exception:
            pass

    def __repr__(self):
        return "<Section %s %d bytes>" % (self.name, self.layout["total_size"])


class SlotRing(object):
    """One direction of the wire: a fixed-size FIFO of payload slots.

    ``direction`` is ``"req"`` (client writes, server reads) or ``"resp"``
    (server writes, client reads). Both ends construct the *same* object; each
    end simply calls only the method that belongs to its role.
    """

    def __init__(self, section, direction):
        if direction not in ("req", "resp"):
            raise ValueError("direction must be 'req' or 'resp'")
        self.section = section
        self.direction = direction
        self.slot_count = int(section.layout["slot_count"])
        self.slot_size = int(section.layout["slot_size"])
        self.max_payload = shm_layout.max_payload_size(self.slot_size)
        self.region = int(section.layout["%s_offset" % direction])
        offsets = shm_layout.HEADER_OFFSETS
        self._head_off = offsets["%s_head" % direction]
        self._tail_off = offsets["%s_tail" % direction]
        self._overflow_off = offsets["%s_overflow" % direction]

    def pending(self):
        """How many messages the consumer has not taken yet."""
        return self.section.u64(self._head_off) - self.section.u64(self._tail_off)

    def reset(self):
        """Drop everything. Only the peer that owns the section calls this, and
        only while no other producer is attached (startup, or reclaiming a dead
        client's channel)."""
        self.section.set_u64(self._head_off, 0)
        self.section.set_u64(self._tail_off, 0)

    def publish(self, payload):
        """Write one frame. Returns True, or False when the ring is full.

        ``False`` is backpressure, not an error: the caller decides whether to
        wait for the peer to drain or to give up. An oversized frame *is* an
        error -- it can never fit, so waiting would be a lie.
        """
        if len(payload) > self.max_payload:
            raise TransportError(
                "shm frame is %d bytes, one slot holds %d (slot_size=%d). Raise "
                "shm slot_size on BOTH sides -- they must match."
                % (len(payload), self.max_payload, self.slot_size))
        section = self.section
        head = section.u64(self._head_off)
        tail = section.u64(self._tail_off)
        if head - tail >= self.slot_count:
            section.set_u64(self._overflow_off,
                            section.u64(self._overflow_off) + 1)
            return False
        offset = self.region + (head % self.slot_count) * self.slot_size
        # State first (still EMPTY), then the bytes, then READY -- so a reader
        # can never see READY on a half-written slot.
        _SLOT.pack_into(section.mm, offset, shm_layout.SLOT_EMPTY,
                        len(payload), head + 1)
        start = offset + shm_layout.SLOT_HEADER_SIZE
        section.mm[start:start + len(payload)] = payload
        _U32.pack_into(section.mm, offset, shm_layout.SLOT_READY)
        section.set_u64(self._head_off, head + 1)
        return True

    def consume(self):
        """Return the next frame as ``bytes``, or None when the ring is empty."""
        section = self.section
        head = section.u64(self._head_off)
        tail = section.u64(self._tail_off)
        if tail >= head:
            return None
        offset = self.region + (tail % self.slot_count) * self.slot_size
        state, length, _seq = _SLOT.unpack_from(section.mm, offset)
        if state != shm_layout.SLOT_READY:
            # Cannot happen under the SPSC contract; returning "empty" keeps a
            # corrupt slot from being parsed as a frame.
            return None
        if length > self.max_payload:
            section.set_u32(shm_layout.HEADER_OFFSETS["last_error"], 1)
            section.set_u64(self._tail_off, tail + 1)
            return None
        start = offset + shm_layout.SLOT_HEADER_SIZE
        payload = bytes(section.mm[start:start + length])
        _U32.pack_into(section.mm, offset, shm_layout.SLOT_EMPTY)
        section.set_u64(self._tail_off, tail + 1)
        return payload
