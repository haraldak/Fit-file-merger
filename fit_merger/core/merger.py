"""fit_merger.core.merger – Merge FIT activity files into one."""

import struct
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from .parser import (
    BASE_TYPE_INFO,
    MESG_ACTIVITY,
    MESG_EVENT,
    MESG_FILE_ID,
    MESG_LAP,
    MESG_RECORD,
    MESG_SESSION,
    Definition,
    FitParser,
    FitRecord,
    crc16,
)

# ═══════════════════════════════════════════════════════════════
#  Low-level helpers
# ═══════════════════════════════════════════════════════════════


def _patch(raw: bytearray, defn: Definition, field_num: int, new_val: Any) -> None:
    """Patch a single field value in-place in a data record's raw bytes."""
    if new_val is None:
        return
    pos    = 1      # skip record-header byte
    endian = defn.endian
    for f in defn.fields:
        if f.num == field_num:
            info = BASE_TYPE_INFO.get(f.base_type, ('B', 1, 0xFF))
            fmt, base_sz, _ = info
            if f.size == base_sz:
                try:
                    struct.pack_into(endian + fmt, raw, pos, new_val)
                except struct.error:
                    pass
            return
        pos += f.size


def _find_last(recs: List[FitRecord], global_num: int) -> Optional[FitRecord]:
    for r in reversed(recs):
        if not r.is_def and r.global_num == global_num:
            return r
    return None


def _fv(rec: Optional[FitRecord], field_num: int, default: Any = 0) -> Any:
    if rec and rec.values:
        v = rec.values.get(field_num)
        return v if v is not None else default
    return default


def _safe_add16(a: int, b: int) -> int:
    """Sum two uint16 values, treating 0xFFFF as 'invalid/zero'."""
    a = 0 if a == 0xFFFF else a
    b = 0 if b == 0xFFFF else b
    return min(a + b, 0xFFFE)


def _safe_add32(a: int, b: int) -> int:
    """Sum two uint32 values, treating 0xFFFFFFFF as 'invalid/zero'."""
    a = 0 if a == 0xFFFFFFFF else a
    b = 0 if b == 0xFFFFFFFF else b
    return min(a + b, 0xFFFFFFFE)


def fmt_time(ms: int) -> str:
    """Format milliseconds as H:MM:SS or M:SS."""
    s = ms // 1000
    h, rem = divmod(s, 3600)
    m, s   = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ═══════════════════════════════════════════════════════════════
#  Merge
# ═══════════════════════════════════════════════════════════════


def merge(data1: bytes, data2: bytes, remove_overlaps: bool = True) -> bytes:
    """Merge two raw FIT file byte strings into one and return the result."""
    p1 = FitParser(data1)
    p2 = FitParser(data2)
    r1 = p1.parse()
    r2 = p2.parse()

    # ── locate summary records ────────────────────────────────

    sess1 = _find_last(r1, MESG_SESSION)
    sess2 = _find_last(r2, MESG_SESSION)
    act1  = _find_last(r1, MESG_ACTIVITY)
    act2  = _find_last(r2, MESG_ACTIVITY)

    if sess1 is None:
        raise ValueError("file1 contains no session message")
    if sess2 is None:
        raise ValueError("file2 contains no session message")

    # ── detect gaps in file1's GPS record stream ──────────────
    # A gap is a stretch where file1 has no MESG_RECORD but file2 does.
    # We stitch those segments in rather than discarding them.

    _RECORD_GAP_SECS = 30
    _f1_rec_ts: List[int] = sorted(
        _fv(r, 253) for r in r1
        if not r.is_def and r.global_num == MESG_RECORD and _fv(r, 253) > 0
    )
    _f2_rec_ts: List[int] = sorted(
        _fv(r, 253) for r in r2
        if not r.is_def and r.global_num == MESG_RECORD and _fv(r, 253) > 0
    )
    _f1_gaps: List[Tuple[int, int]] = [
        (_f1_rec_ts[i], _f1_rec_ts[i + 1])
        for i in range(len(_f1_rec_ts) - 1)
        if _f1_rec_ts[i + 1] - _f1_rec_ts[i] > _RECORD_GAP_SECS
    ]

    def _in_f1_gap(ts: int) -> bool:
        return any(gs < ts < ge for gs, ge in _f1_gaps)

    _gap_fill_recs: List[FitRecord] = (
        [r for r in r2
         if not r.is_def and r.global_num == MESG_RECORD and _in_f1_gap(_fv(r, 253))]
        if _f1_gaps else []
    )

    # ── detect time overlap between the two files ──────────────
    # When files share a time range we must NOT sum their stats —
    # that would double-count distance/calories for the shared period.

    _f1_ts_start = _f1_rec_ts[0]  if _f1_rec_ts else 0
    _f1_ts_end   = _f1_rec_ts[-1] if _f1_rec_ts else 0
    _f2_ts_start = _f2_rec_ts[0]  if _f2_rec_ts else 0
    _f2_ts_end   = _f2_rec_ts[-1] if _f2_rec_ts else 0
    _files_overlap = (
        _f1_ts_end > 0 and _f2_ts_end > 0
        and _f2_ts_start < _f1_ts_end
        and _f1_ts_start < _f2_ts_end
    )

    # ── compute merged session field values ───────────────────
    # Field numbers (FIT profile):
    #   253 = timestamp     2  = start_time
    #    7  = total_elapsed_time (uint32, ms)
    #    8  = total_timer_time   (uint32, ms)
    #    9  = total_distance     (uint32, cm/100)
    #   11  = total_calories     (uint16)
    #   22  = total_ascent       (uint16, m)
    #   23  = total_descent      (uint16, m)
    #   25  = first_lap_index    (uint16)
    #   26  = num_laps           (uint16)

    ts1 = _fv(sess1, 253); ts2 = _fv(sess2, 253)
    merged_ts      = max(ts1, ts2)
    merged_start   = min(_fv(sess1, 2, ts1), _fv(sess2, 2, ts2))
    merged_ascent  = _safe_add16(_fv(sess1, 22), _fv(sess2, 22))
    merged_descent = _safe_add16(_fv(sess1, 23), _fv(sess2, 23))

    if _files_overlap:
        # Overlapping time ranges — take the larger value to avoid double-counting
        # the shared segment (one file is missing a portion the other covers).
        merged_elapsed = max(_fv(sess1, 7), _fv(sess2, 7))
        merged_timer   = max(_fv(sess1, 8), _fv(sess2, 8))
        merged_dist    = max(_fv(sess1, 9), _fv(sess2, 9))
        merged_cal     = max(_fv(sess1, 11), _fv(sess2, 11))
    else:
        merged_elapsed = _safe_add32(_fv(sess1, 7), _fv(sess2, 7))
        merged_timer   = _safe_add32(_fv(sess1, 8), _fv(sess2, 8))
        merged_dist    = _safe_add32(_fv(sess1, 9), _fv(sess2, 9))
        merged_cal     = _safe_add16(_fv(sess1, 11), _fv(sess2, 11))

    laps1 = sum(1 for r in r1 if not r.is_def and r.global_num == MESG_LAP)
    laps2 = sum(1 for r in r2 if not r.is_def and r.global_num == MESG_LAP)
    if laps1 == 0:
        laps1 = _fv(sess1, 26, 1)
    if laps2 == 0:
        laps2 = _fv(sess2, 26, 1)
    merged_num_laps = laps1 + laps2

    # ── patch session + activity from file1 in-place ─────────

    def _make_patched_sess() -> bytes:
        if not sess1.defn:
            return sess1.raw
        p = bytearray(sess1.raw)
        d = sess1.defn
        _patch(p, d, 253, merged_ts)
        _patch(p, d, 2,   merged_start)
        _patch(p, d, 7,   merged_elapsed)
        _patch(p, d, 8,   merged_timer)
        _patch(p, d, 9,   merged_dist)
        _patch(p, d, 11,  merged_cal)
        _patch(p, d, 22,  merged_ascent)
        _patch(p, d, 23,  merged_descent)
        _patch(p, d, 25,  0)               # first_lap_index
        _patch(p, d, 26,  merged_num_laps)
        return bytes(p)

    def _make_patched_act() -> bytes:
        if not act1 or not act1.defn:
            return act1.raw if act1 else b''
        act_ts    = max(_fv(act1, 253), _fv(act2, 253) if act2 else 0)
        act_timer = _safe_add32(_fv(act1, 0), _fv(act2, 0) if act2 else 0)
        tz_offset = (_fv(act2, 5) - _fv(act2, 253)) if act2 else (_fv(act1, 5) - _fv(act1, 253))
        p = bytearray(act1.raw)
        d = act1.defn
        _patch(p, d, 253, act_ts)
        _patch(p, d, 0,   act_timer)
        _patch(p, d, 1,   1)                       # num_sessions = 1 (merged)
        _patch(p, d, 5,   act_ts + tz_offset)      # local_timestamp
        return bytes(p)

    # ── identify singleton "header" messages to skip from file2 ──

    _f1_counts = Counter(r.global_num for r in r1 if not r.is_def)
    _f2_counts = Counter(r.global_num for r in r2 if not r.is_def)
    _time_series = {MESG_LAP, MESG_RECORD, MESG_EVENT}
    _skip_from_f2 = {
        gnum for gnum in _f2_counts
        if _f2_counts[gnum] == 1
        and _f1_counts.get(gnum, 0) == 1
        and gnum not in _time_series
        and gnum not in (MESG_FILE_ID, MESG_SESSION, MESG_ACTIVITY)
    }

    # ── boundary trim logic ───────────────────────────────────

    _core = {MESG_LAP, MESG_RECORD, MESG_EVENT, MESG_SESSION, MESG_ACTIVITY, 23}

    last_gps_ts_r1 = max(
        (_fv(r, 253) for r in r1 if not r.is_def and r.global_num == MESG_RECORD),
        default=0,
    )

    _f2_init_exclusions = {288, 313}

    def _keep_from_f1(rec: FitRecord) -> bool:
        if rec.is_def:
            return True
        ts = _fv(rec, 253)
        if ts > 0 and ts > last_gps_ts_r1 and rec.global_num not in _core:
            return False
        return True

    def _keep_from_f2(rec: FitRecord) -> bool:
        if rec.is_def:
            return True
        if rec.global_num in _f2_init_exclusions:
            return False
        if remove_overlaps and rec.global_num == MESG_RECORD and last_gps_ts_r1 > 0:
            ts = _fv(rec, 253)
            if ts > 0 and ts <= last_gps_ts_r1:
                return False
        return True

    # ── prepare gap-fill injection ────────────────────────────
    # Gap records are inserted inline at the correct timestamp position.
    # We temporarily override file1's active MESG_RECORD local with
    # file2's definition, emit the gap records using that local, then
    # restore the original file1 definition.  This keeps the output in
    # strict timestamp order so FIT viewers draw a continuous track.

    _gap_fill_ids: set = set()
    _gf_sorted:    List[FitRecord] = []

    if _gap_fill_recs:
        _gf_sorted    = sorted(_gap_fill_recs, key=lambda r: _fv(r, 253))
        _gap_fill_ids = {id(r) for r in _gap_fill_recs}
        _gf_locals    = {r.local_num for r in _gap_fill_recs}

        # Find file2's MESG_RECORD definition for each gap-fill local —
        # the last definition before the first gap record is the active one.
        _f2_rec_def: Dict[int, bytes] = {}
        for rec in r2:
            if rec.is_def and rec.local_num in _gf_locals and rec.global_num == MESG_RECORD:
                _f2_rec_def[rec.local_num] = rec.raw
            if not rec.is_def and rec.global_num == MESG_RECORD and _in_f1_gap(_fv(rec, 253)):
                break  # definition is now fixed for all gap records

    _f1_act_rec_local:   Optional[int]   = None  # active MESG_RECORD local in file1
    _f1_act_rec_def_raw: Optional[bytes] = None  # its most recent definition bytes
    _gf_idx = 0

    def _relabel(raw: bytes, old: int, new: int) -> bytes:
        """Rewrite the local-number nibble in a FIT record header byte."""
        if old == new:
            return raw
        b = bytearray(raw)
        b[0] = (b[0] & 0xF0) | new
        return bytes(b)

    def _inject_before(ts: int) -> None:
        """Emit gap-fill records with timestamps < ts at the current stream position."""
        nonlocal _gf_idx
        if _gf_idx >= len(_gf_sorted) or _f1_act_rec_local is None:
            return
        if _fv(_gf_sorted[_gf_idx], 253) >= ts:
            return  # fast path: nothing to inject before ts

        target = _f1_act_rec_local
        prev_f2_local: Optional[int] = None

        while _gf_idx < len(_gf_sorted) and _fv(_gf_sorted[_gf_idx], 253) < ts:
            rec   = _gf_sorted[_gf_idx]
            f2loc = rec.local_num
            # Emit an override definition whenever the gap-fill local changes
            if f2loc != prev_f2_local and f2loc in _f2_rec_def:
                out.append(_relabel(_f2_rec_def[f2loc], f2loc, target))
                prev_f2_local = f2loc
            out.append(_relabel(rec.raw, f2loc, target))
            _gf_idx += 1

        # Restore file1's original definition for that local
        if prev_f2_local is not None and _f1_act_rec_def_raw is not None:
            out.append(_f1_act_rec_def_raw)

    # ── build output record stream ────────────────────────────

    out: List[bytes] = []
    skip2 = {id(sess2), id(act2)} if act2 else {id(sess2)}

    for rec in r1:
        # Track file1's active MESG_RECORD definition
        if rec.is_def and rec.global_num == MESG_RECORD:
            _f1_act_rec_local   = rec.local_num
            _f1_act_rec_def_raw = rec.raw

        # Inject gap-fill records just before each file1 MESG_RECORD data record
        if not rec.is_def and rec.global_num == MESG_RECORD:
            _inject_before(_fv(rec, 253))

        if rec is sess1:
            out.append(_make_patched_sess())
        elif rec is act1:
            out.append(_make_patched_act())
        elif _keep_from_f1(rec):
            out.append(rec.raw)

    # Drain any gap records that fall after the last file1 MESG_RECORD
    _inject_before(0x7FFFFFFF)

    for rec in r2:
        if id(rec) in skip2:
            continue
        if id(rec) in _gap_fill_ids:  # already injected at correct position
            continue
        if rec.global_num in _skip_from_f2:
            continue
        if rec.global_num in (MESG_FILE_ID, 23):  # device identity stays with file1
            continue
        if not _keep_from_f2(rec):
            continue
        out.append(rec.raw)

    # ── assemble file ─────────────────────────────────────────

    body      = b''.join(out)
    proto_ver = max(p1.proto_ver, p2.proto_ver)
    profile   = max(p1.profile_ver, p2.profile_ver)

    hdr12  = struct.pack('<BBHI4s', 14, proto_ver, profile, len(body), b'.FIT')
    header = hdr12 + struct.pack('<H', crc16(hdr12))

    return header + body + struct.pack('<H', crc16(body))


def merge_all(files: List[bytes], remove_overlaps: bool = True) -> bytes:
    """Merge an ordered list of raw FIT byte strings into a single file."""
    if len(files) < 2:
        raise ValueError("Need at least two files to merge")
    result = files[0]
    for f in files[1:]:
        result = merge(result, f, remove_overlaps=remove_overlaps)
    return result


def get_fit_start_ts(data: bytes) -> int:
    """Return the activity start timestamp (FIT epoch seconds) or 0 on failure."""
    try:
        records = FitParser(data).parse()
        sess = _find_last(records, MESG_SESSION)
        if sess:
            ts = _fv(sess, 2)       # start_time field
            if ts and ts != 0xFFFFFFFF:
                return ts
            ts = _fv(sess, 253)     # fallback: session timestamp
            if ts and ts != 0xFFFFFFFF:
                return ts
        # Last resort: first record message timestamp
        for r in records:
            if not r.is_def and r.global_num == MESG_RECORD:
                ts = _fv(r, 253)
                if ts:
                    return ts
    except Exception:
        pass
    return 0
