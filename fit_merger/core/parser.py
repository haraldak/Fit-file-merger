"""fit_merger.core.parser – Low-level FIT file parsing.

Exposes FitParser, FitRecord, Definition, FieldDef, FIT message-type
constants, and the CRC-16 helper used throughout the package.
"""

import struct
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional, Tuple

# ═══════════════════════════════════════════════════════════════
#  CRC-16 (Garmin FIT variant)
# ═══════════════════════════════════════════════════════════════

_CRC_TABLE = [
    0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
]


def crc16(data: bytes, seed: int = 0) -> int:
    crc = seed
    for byte in data:
        for _ in range(2):
            tmp = _CRC_TABLE[crc & 0xF]
            crc = (crc >> 4) & 0x0FFF
            crc ^= tmp ^ _CRC_TABLE[byte & 0xF]
            byte >>= 4
    return crc


# ═══════════════════════════════════════════════════════════════
#  FIT message-type constants
# ═══════════════════════════════════════════════════════════════

MESG_FILE_ID  = 0
MESG_SESSION  = 18
MESG_LAP      = 19
MESG_RECORD   = 20
MESG_EVENT    = 21
MESG_ACTIVITY = 34

# base_type_byte -> (struct_fmt, byte_size, invalid_sentinel)
BASE_TYPE_INFO: Dict[int, Tuple[str, int, Any]] = {
    0x00: ('B', 1,  0xFF),
    0x01: ('b', 1,  0x7F),
    0x02: ('B', 1,  0xFF),
    0x83: ('h', 2,  0x7FFF),
    0x84: ('H', 2,  0xFFFF),
    0x85: ('i', 4,  0x7FFFFFFF),
    0x86: ('I', 4,  0xFFFFFFFF),
    0x07: ('B', 1,  0x00),
    0x88: ('f', 4,  None),
    0x89: ('d', 8,  None),
    0x0A: ('B', 1,  0x00),
    0x8B: ('H', 2,  0x0000),
    0x8C: ('I', 4,  0x00000000),
    0x0D: ('B', 1,  0xFF),
    0x8E: ('q', 8,  0x7FFFFFFFFFFFFFFF),
    0x8F: ('Q', 8,  0xFFFFFFFFFFFFFFFF),
    0x90: ('Q', 8,  0x0000000000000000),
}

# ═══════════════════════════════════════════════════════════════
#  Data structures
# ═══════════════════════════════════════════════════════════════


@dataclass
class FieldDef:
    num:       int
    size:      int
    base_type: int


@dataclass
class Definition:
    local_num:  int
    global_num: int
    arch:       int   # 0 = LE, 1 = BE
    fields:     List[FieldDef]
    has_dev:    bool = False
    dev_fields: List[Tuple[int, int, int]] = dc_field(default_factory=list)

    @property
    def endian(self) -> str:
        return '>' if self.arch else '<'

    @property
    def record_data_size(self) -> int:
        return sum(f.size for f in self.fields) + sum(s for _, s, _ in self.dev_fields)


@dataclass
class FitRecord:
    is_def:     bool
    local_num:  int
    global_num: int
    raw:        bytes
    values:     Optional[Dict[int, Any]] = None
    defn:       Optional[Definition]     = None   # active definition (data records only)


# ═══════════════════════════════════════════════════════════════
#  Parser
# ═══════════════════════════════════════════════════════════════


class FitParser:
    def __init__(self, data: bytes):
        if len(data) < 12:
            raise ValueError("File too short to be a FIT file")
        if data[8:12] != b'.FIT':
            raise ValueError("Not a FIT file (missing .FIT magic)")
        hdr_size  = data[0]
        data_size = struct.unpack_from('<I', data, 4)[0]
        self.data        = data
        self.proto_ver   = data[1]
        self.profile_ver = struct.unpack_from('<H', data, 2)[0]
        self.data_start  = hdr_size
        self.data_end    = hdr_size + data_size
        self.defs: Dict[int, Definition] = {}

    def parse(self) -> List[FitRecord]:
        pos  = self.data_start
        recs: List[FitRecord] = []
        while pos < self.data_end:
            hdr = self.data[pos]
            if hdr & 0x80:                      # compressed timestamp header
                rec, pos = self._read_compressed(pos)
            elif hdr & 0x40:                    # definition message
                rec, pos = self._read_definition(pos, hdr)
            else:                               # data message
                rec, pos = self._read_data(pos, hdr)
            recs.append(rec)
        return recs

    # ── compressed timestamp ──────────────────────────────────

    def _read_compressed(self, pos: int) -> Tuple[FitRecord, int]:
        hdr       = self.data[pos]
        local_num = (hdr >> 5) & 0x3
        defn      = self.defs.get(local_num)
        if defn is None:
            raise ValueError(f"Compressed record references undefined local type {local_num}")
        end    = pos + 1 + defn.record_data_size
        raw    = self.data[pos:end]
        values = self._decode(defn, pos + 1)
        return FitRecord(False, local_num, defn.global_num, raw, values, defn), end

    # ── definition message ────────────────────────────────────

    def _read_definition(self, pos: int, hdr: int) -> Tuple[FitRecord, int]:
        has_dev    = bool(hdr & 0x20)
        local_num  = hdr & 0x0F
        start      = pos
        pos       += 2                           # header + reserved
        arch       = self.data[pos]; pos += 1
        endian     = '>' if arch else '<'
        global_num = struct.unpack_from(endian + 'H', self.data, pos)[0]; pos += 2
        n_fields   = self.data[pos]; pos += 1

        fields = []
        for _ in range(n_fields):
            fields.append(FieldDef(self.data[pos], self.data[pos + 1], self.data[pos + 2]))
            pos += 3

        dev_fields: List[Tuple[int, int, int]] = []
        if has_dev:
            n_dev = self.data[pos]; pos += 1
            for _ in range(n_dev):
                dev_fields.append((self.data[pos], self.data[pos + 1], self.data[pos + 2]))
                pos += 3

        defn = Definition(local_num, global_num, arch, fields, has_dev, dev_fields)
        self.defs[local_num] = defn
        raw = self.data[start:pos]
        return FitRecord(True, local_num, global_num, raw), pos

    # ── data message ──────────────────────────────────────────

    def _read_data(self, pos: int, hdr: int) -> Tuple[FitRecord, int]:
        local_num = hdr & 0x0F
        defn      = self.defs.get(local_num)
        if defn is None:
            raise ValueError(f"Data record references undefined local type {local_num}")
        end    = pos + 1 + defn.record_data_size
        raw    = self.data[pos:end]
        values = self._decode(defn, pos + 1)
        return FitRecord(False, local_num, defn.global_num, raw, values, defn), end

    # ── field decoder ─────────────────────────────────────────

    def _decode(self, defn: Definition, data_pos: int) -> Dict[int, Any]:
        vals   = {}
        pos    = data_pos
        endian = defn.endian
        for f in defn.fields:
            info = BASE_TYPE_INFO.get(f.base_type, ('B', 1, 0xFF))
            fmt, base_sz, _ = info
            if f.size > base_sz or (fmt in ('B', 'b') and f.size > 1):
                vals[f.num] = bytes(self.data[pos:pos + f.size])
            else:
                vals[f.num] = struct.unpack_from(endian + fmt, self.data, pos)[0]
            pos += f.size
        return vals
