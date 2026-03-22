"""fit_merger.ui.app – Tkinter GUI for FIT Activity Merger (redesigned)."""

from __future__ import annotations

import dataclasses
import datetime
import math
import os
import re
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, List, Optional, Tuple

from fit_merger.core.merger import _find_last, _fv, fmt_time, get_fit_start_ts, merge_all
from fit_merger.core.parser import FitParser, MESG_RECORD, MESG_SESSION

# ── Optional deps ──────────────────────────────────────────────────────────────
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _DND_AVAILABLE = True
except ImportError:
    _DND_AVAILABLE = False

try:
    import tkintermapview  # type: ignore[import]
    _MAP_AVAILABLE = True
except ImportError:
    _MAP_AVAILABLE = False

try:
    from PIL import Image, ImageDraw, ImageFont
    from PIL.ImageTk import PhotoImage as _PILPhoto  # type: ignore[import]
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

# ── Constants ──────────────────────────────────────────────────────────────────
_FIT_EPOCH       = datetime.datetime(1989, 12, 31, tzinfo=datetime.timezone.utc)
WINDOW_TITLE     = "FIT Activity Merger"
PAD              = 8
FIT_FILETYPES    = [("Garmin FIT files", "*.fit"), ("All files", "*.*")]
_ARROW_INTERVAL  = 400   # metres between direction arrows
_GAP_THRESHOLD_M = 200   # metres; gaps below this are not flagged

ACTIVITY_COLORS = [
    "#4A90D9",  # blue
    "#5CB85C",  # green
    "#9B59B6",  # purple
    "#E67E22",  # orange
    "#E74C3C",  # red
    "#1ABC9C",  # teal
    "#F39C12",  # amber
]

_SEMICIRCLE_TO_DEG = 180.0 / (2 ** 31)
_INVALID_SINT32    = 0x7FFFFFFF

# FIT sport enum → display label (field 5 of SESSION message)
_SPORT_NAMES: dict = {
    0:  "Generic",    1:  "Running",    2:  "Cycling",
    3:  "Transition", 4:  "Fitness",    5:  "Swimming",
    6:  "Basketball", 7:  "Soccer",     8:  "Tennis",
    9:  "Football",   10: "Training",   11: "Walking",
    12: "XC Skiing",  13: "Skiing",     14: "Snowboard",
    15: "Rowing",     16: "Mountaineer",17: "Hiking",
    18: "Multisport", 19: "Paddling",
}

# ── Data model ─────────────────────────────────────────────────────────────────

# gps_track stores (lat, lon, fit_timestamp); fit_timestamp=0 when unknown
GpsPoint = Tuple[float, float, int]


@dataclasses.dataclass
class FileInfo:
    path:         str
    ts:             int                # FIT epoch seconds – activity start (sort key)
    display_datetime: str              # "YYYY-MM-DD HH:MM"
    sport:          str                # e.g. "Running"
    distance_km:  float
    timer_ms:     int
    gps_track:    List[GpsPoint]       # [(lat, lon, fit_ts), …]
    end_ts:       int                  # FIT timestamp of last GPS point (0 = unknown)


def _load_file_info(path: str) -> FileInfo:
    with open(path, "rb") as fh:
        data = fh.read()
    recs = FitParser(data).parse()

    ts = get_fit_start_ts(data)
    if ts:
        dt               = (_FIT_EPOCH + datetime.timedelta(seconds=ts)).astimezone()
        display_datetime = dt.strftime("%Y-%m-%d %H:%M")
    else:
        display_datetime = "—"

    sess = _find_last(recs, MESG_SESSION)
    distance_km = _fv(sess, 9, 0) / 100_000 if sess else 0.0
    timer_ms    = _fv(sess, 8, 0)            if sess else 0
    sport       = _SPORT_NAMES.get(_fv(sess, 5, 0), "Activity") if sess else "Activity"

    gps_track: List[GpsPoint] = []
    for r in recs:
        if r.is_def or r.global_num != MESG_RECORD:
            continue
        lat_sc, lon_sc = r.values.get(0), r.values.get(1)
        if lat_sc is None or lon_sc is None:
            continue
        if lat_sc == _INVALID_SINT32 or lon_sc == _INVALID_SINT32:
            continue
        lat, lon = lat_sc * _SEMICIRCLE_TO_DEG, lon_sc * _SEMICIRCLE_TO_DEG
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            fit_ts = _fv(r, 253, 0)
            gps_track.append((lat, lon, fit_ts))

    end_ts = gps_track[-1][2] if gps_track else 0

    return FileInfo(path=path, ts=ts, display_datetime=display_datetime, sport=sport,
                    distance_km=distance_km, timer_ms=timer_ms,
                    gps_track=gps_track, end_ts=end_ts)


def _coords(track: List[GpsPoint]) -> List[Tuple[float, float]]:
    """Strip timestamps – returns plain (lat, lon) list for map API."""
    return [(lat, lon) for lat, lon, _ in track]


# ── Geo helpers ────────────────────────────────────────────────────────────────

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin(math.radians(lat2 - lat1) / 2) ** 2
         + math.cos(phi1) * math.cos(phi2)
         * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(max(0.0, min(1.0, a))))


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _detect_gaps(files: List[FileInfo]) -> List[Tuple[int, int, float]]:
    gaps = []
    for i in range(len(files) - 1):
        a, b = files[i], files[i + 1]
        if not a.gps_track or not b.gps_track:
            continue
        a_lat, a_lon, _ = a.gps_track[-1]
        b_lat, b_lon, _ = b.gps_track[0]
        d = _haversine_m(a_lat, a_lon, b_lat, b_lon)
        if d > _GAP_THRESHOLD_M:
            gaps.append((i, i + 1, d))
    return gaps


def _sample_arrows(track: List[GpsPoint]) -> List[Tuple[float, float, float]]:
    """Return [(lat, lon, bearing), …] sampled every _ARROW_INTERVAL metres."""
    if len(track) < 2:
        return []
    result: List[Tuple[float, float, float]] = []
    accum, next_at = 0.0, _ARROW_INTERVAL * 0.4
    for i in range(len(track) - 1):
        lat1, lon1, _ = track[i]
        lat2, lon2, _ = track[i + 1]
        accum += _haversine_m(lat1, lon1, lat2, lon2)
        if accum >= next_at:
            result.append((lat1, lon1, _bearing_deg(lat1, lon1, lat2, lon2)))
            next_at = accum + _ARROW_INTERVAL
    return result


# ── PIL image helpers ──────────────────────────────────────────────────────────

def _dim_color(hex_color: str, factor: float = 0.55) -> str:
    r, g, b = int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
    return "#{:02x}{:02x}{:02x}".format(
        int(r + (255 - r) * factor),
        int(g + (255 - g) * factor),
        int(b + (255 - b) * factor),
    )


def _pil_font(size: int = 11) -> Any:
    for path in ("C:/Windows/Fonts/segoeui.ttf", "/System/Library/Fonts/Helvetica.ttc"):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def _make_circle_marker(color_hex: str, number: int, size: int = 20) -> Any:
    if not _PIL_AVAILABLE:
        return None
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([1, 1, size - 2, size - 2], fill=color_hex, outline="white", width=2)
    font = _pil_font(10)
    text = str(number)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1]),
              text, fill="white", font=font)
    return _PILPhoto(img)


def _make_arrow_marker(color_hex: str, bearing: float, size: int = 13) -> Any:
    if not _PIL_AVAILABLE:
        return None
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx = size / 2
    draw.polygon([(cx, 0), (size - 1, size - 1), (cx, size * 0.55), (0, size - 1)],
                 fill=color_hex)
    img = img.rotate(-bearing, expand=False, resample=Image.BICUBIC)  # type: ignore[attr-defined]
    return _PILPhoto(img)


# ── Misc helpers ───────────────────────────────────────────────────────────────

def _parse_dnd_paths(raw: str) -> List[str]:
    return [m[0] or m[1] for m in re.findall(r'\{([^}]+)\}|(\S+)', raw)]


def _dir_of(path: str) -> str:
    if path:
        d = os.path.dirname(path)
        if os.path.isdir(d):
            return d
    return os.path.expanduser("~")


def _color_for(index: int) -> str:
    return ACTIVITY_COLORS[index % len(ACTIVITY_COLORS)]


def _zip_sort(*lists: list, key: Any) -> tuple:
    combined = sorted(zip(*lists), key=lambda t: key(t[0]))
    if not combined:
        return tuple([] for _ in lists)
    return tuple(map(list, zip(*combined)))


# ── App base ───────────────────────────────────────────────────────────────────
_AppBase = TkinterDnD.Tk if _DND_AVAILABLE else tk.Tk


# ── Main window ────────────────────────────────────────────────────────────────

class FitMergerApp(_AppBase):  # type: ignore[misc]

    def __init__(self) -> None:
        super().__init__()
        self.title(WINDOW_TITLE)
        self.resizable(True, True)
        self.minsize(500, 600)

        self._files:        List[FileInfo] = []
        self._file_visible: List[bool]     = []
        self._selected_idx: Optional[int]  = None

        self.var_output      = tk.StringVar()
        self.var_rm_overlaps = tk.BooleanVar(value=True)
        self.var_show_arrows = tk.BooleanVar(value=False)
        self._options_open   = True

        self._map_objects:  list = []
        self._pin_images:   list = []
        self._arrow_images: list = []

        self._build_ui()
        self.var_output.trace_add("write", self._update_merge_button)
        self.var_show_arrows.trace_add("write",  lambda *_: self._refresh_map(fit=False))
        self.var_rm_overlaps.trace_add("write",  lambda *_: self._refresh_map(fit=False))

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.configure(bg="#EBEBEB")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        top = tk.Frame(self, bg="#EBEBEB")
        top.grid(row=0, column=0, sticky="ew", padx=PAD, pady=(PAD, 3))
        top.columnconfigure(0, weight=1)
        self._build_files_section(top)

        mid = tk.Frame(self, bg="#EBEBEB")
        mid.grid(row=1, column=0, sticky="nsew", padx=PAD, pady=3)
        mid.columnconfigure(0, weight=1)
        mid.rowconfigure(0, weight=1)
        self._build_map_section(mid)

        bot = tk.Frame(self, bg="#EBEBEB")
        bot.grid(row=2, column=0, sticky="ew", padx=PAD, pady=(3, PAD))
        bot.columnconfigure(0, weight=1)
        # self._build_options_section(bot)
        self._build_output_section(bot)
        self._build_merge_button(bot)
        self._build_result_section(bot)

    # ── Files section ──────────────────────────────────────────────────────────

    def _build_files_section(self, parent: tk.Frame) -> None:
        outer = tk.Frame(parent, bg="white",
                         highlightbackground="#D0D0D0", highlightthickness=1)
        outer.pack(fill="x", pady=(0, 6))

        hdr = tk.Frame(outer, bg="#F5F5F5")
        hdr.pack(fill="x")
        tk.Label(hdr, text="\U0001f5c2  Files", bg="#F5F5F5",
                 font=("Segoe UI", 10, "bold"), anchor="w", padx=8, pady=5).pack(side="left")
        ttk.Button(hdr, text="Sort by time \u25be",
                   command=self._sort_by_time).pack(side="right", padx=6, pady=3)

        self._files_container = tk.Frame(outer, bg="white", padx=8, pady=4)
        self._files_container.pack(fill="x")

        self._gap_frame = tk.Frame(outer, bg="#FFF8E1")

        if _DND_AVAILABLE:
            outer.drop_target_register(DND_FILES)
            outer.dnd_bind("<<Drop>>", self._on_drop)

        self._refresh_files_ui()

    def _refresh_files_ui(self) -> None:
        while len(self._file_visible) < len(self._files):
            self._file_visible.append(True)
        self._file_visible = self._file_visible[:len(self._files)]
        if self._selected_idx is not None and self._selected_idx >= len(self._files):
            self._selected_idx = None

        for w in self._files_container.winfo_children():
            w.destroy()

        if not self._files:
            self._build_drop_zone(self._files_container)
        else:
            for i, fi in enumerate(self._files):
                self._build_file_row(self._files_container, i, fi)

        btn_row = tk.Frame(self._files_container, bg="white")
        btn_row.pack(fill="x", pady=(6, 2))
        ttk.Button(btn_row, text="Add Files\u2026",
                   command=self._add_files).pack(side="left", padx=(0, 4))
        if self._files:
            ttk.Button(btn_row, text="Clear All",
                       command=self._clear_files).pack(side="left")

        self._update_gap_warning()

    def _build_drop_zone(self, parent: tk.Frame) -> None:
        zone = tk.Frame(parent, bg="#FAFAFA", relief="groove", bd=1, height=80)
        zone.pack(fill="x", pady=(2, 2))
        zone.pack_propagate(False)
        msg = "Drop .fit files here" if _DND_AVAILABLE else "Click 'Add Files\u2026' to select .fit files"
        lbl = tk.Label(zone, text=msg, bg="#FAFAFA", fg="#AAAAAA", font=("Segoe UI", 10))
        lbl.place(relx=0.5, rely=0.5, anchor="center")
        if _DND_AVAILABLE:
            zone.drop_target_register(DND_FILES)
            zone.dnd_bind("<<Drop>>", self._on_drop)
            lbl.drop_target_register(DND_FILES)
            lbl.dnd_bind("<<Drop>>", self._on_drop)

    def _build_file_row(self, parent: tk.Frame, idx: int, fi: FileInfo) -> None:
        color    = _color_for(idx)
        visible  = self._file_visible[idx]
        selected = (self._selected_idx == idx)
        row_bg   = "#EBF3FB" if selected else "white"

        row = tk.Frame(parent, bg=row_bg, cursor="hand2")
        row.pack(fill="x", pady=1)

        def _on_row_click(e, i=idx):
            self._select_file(i)

        # ── Index ──
        lbl_num = tk.Label(row, text=f"{idx + 1}.", bg=row_bg, width=3,
                           anchor="e", font=("Segoe UI", 9), cursor="hand2")
        lbl_num.pack(side="left")
        lbl_num.bind("<Button-1>", _on_row_click)

        # ── Dot ──
        dot = tk.Canvas(row, width=10, height=10, bg=row_bg, highlightthickness=0, cursor="hand2")
        dot.pack(side="left", padx=(3, 5))
        if visible:
            dot.create_oval(1, 1, 9, 9, fill=color, outline=color)
        else:
            dot.create_oval(1, 1, 9, 9, fill="", outline="#CCCCCC", width=1)
        dot.bind("<Button-1>", _on_row_click)

        # ── Filename ──
        lbl_name = tk.Label(row, text=os.path.basename(fi.path), bg=row_bg, anchor="w",
                            font=("Segoe UI", 9), cursor="hand2",
                            fg="#444444" if visible else "#AAAAAA")
        lbl_name.pack(side="left", fill="x", expand=True)
        lbl_name.bind("<Button-1>", _on_row_click)

        # ── Sport type ──
        lbl_sport = tk.Label(row, text=fi.sport, bg=row_bg, anchor="e",
                             font=("Segoe UI", 8), cursor="hand2",
                             fg=color if visible else "#CCCCCC")
        lbl_sport.pack(side="left", padx=(4, 4))
        lbl_sport.bind("<Button-1>", _on_row_click)

        # ── Date/Time ──
        lbl_dt = tk.Label(row, text=fi.display_datetime, bg=row_bg, width=15,
                          anchor="e", font=("Segoe UI", 9), cursor="hand2",
                          fg="#666666" if visible else "#AAAAAA")
        lbl_dt.pack(side="left", padx=(0, 4))
        lbl_dt.bind("<Button-1>", _on_row_click)

        # ── Distance ──
        dist_str = f"{fi.distance_km:.2f} km" if fi.distance_km else ""
        lbl_dist = tk.Label(row, text=dist_str, bg=row_bg, width=9, anchor="e",
                            font=("Segoe UI", 9), cursor="hand2",
                            fg="#444444" if visible else "#AAAAAA")
        lbl_dist.pack(side="left", padx=(4, 4))
        lbl_dist.bind("<Button-1>", _on_row_click)

        # ── Duration ──
        dur_str = fmt_time(fi.timer_ms) if fi.timer_ms else ""
        lbl_dur = tk.Label(row, text=dur_str, bg=row_bg, width=8, anchor="e",
                           font=("Segoe UI", 9), cursor="hand2",
                           fg="#444444" if visible else "#AAAAAA")
        lbl_dur.pack(side="left", padx=(0, 4))
        lbl_dur.bind("<Button-1>", _on_row_click)

        # ── Avg pace ──
        if fi.distance_km > 0 and fi.timer_ms > 0:
            pace_s = (fi.timer_ms / 1000) / fi.distance_km
            pace_str = f"{int(pace_s // 60)}:{int(pace_s % 60):02d}/km"
        else:
            pace_str = ""
        lbl_pace = tk.Label(row, text=pace_str, bg=row_bg, width=8, anchor="e",
                            font=("Segoe UI", 9), cursor="hand2",
                            fg="#666666" if visible else "#AAAAAA")
        lbl_pace.pack(side="left", padx=(0, 4))
        lbl_pace.bind("<Button-1>", _on_row_click)

        # ── Eye toggle ──
        eye_fg = color if visible else "#CCCCCC"
        tk.Button(row, text="\U0001f441", width=2, relief="flat", bd=0,
                  bg=row_bg, fg=eye_fg, font=("Segoe UI", 9), cursor="hand2",
                  command=lambda i=idx: self._toggle_visibility(i)).pack(side="left", padx=(0, 1))

        # ── Up / Down ──
        up_state = "normal" if idx > 0                    else "disabled"
        dn_state = "normal" if idx < len(self._files) - 1 else "disabled"
        tk.Button(row, text="\u2191", width=2, relief="flat", bg="#EEEEEE",
                  state=up_state,
                  command=lambda i=idx: self._move_file(i, -1)).pack(side="left", padx=1)
        tk.Button(row, text="\u2193", width=2, relief="flat", bg="#EEEEEE",
                  state=dn_state,
                  command=lambda i=idx: self._move_file(i, +1)).pack(side="left", padx=(0, 1))

        # ── Remove (×) ──
        tk.Button(row, text="\u00d7", width=2, relief="flat", bd=0,
                  bg=row_bg, fg="#CC4444", font=("Segoe UI", 11, "bold"), cursor="hand2",
                  activeforeground="#AA0000", activebackground=row_bg,
                  command=lambda i=idx: self._remove_file(i)).pack(side="left", padx=(1, 0))

    def _update_gap_warning(self) -> None:
        self._gap_frame.pack_forget()
        for w in self._gap_frame.winfo_children():
            w.destroy()
        gaps = _detect_gaps(self._files)
        if not gaps:
            return
        i, j, dist_m = gaps[0]
        name_a = os.path.basename(self._files[i].path)
        name_b = os.path.basename(self._files[j].path)
        tk.Label(self._gap_frame,
                 text=f"\u26a0  Gap detected between {name_a} and {name_b} (approx. {dist_m:.0f} m)",
                 bg="#FFF8E1", fg="#856404", font=("Segoe UI", 9),
                 anchor="w", padx=8, pady=5, wraplength=420).pack(fill="x")
        self._gap_frame.pack(fill="x", pady=(0, 4))

    # ── Map section ────────────────────────────────────────────────────────────

    def _build_map_section(self, parent: tk.Frame) -> None:
        outer = tk.Frame(parent, bg="white",
                         highlightbackground="#D0D0D0", highlightthickness=1)
        outer.grid(row=0, column=0, sticky="nsew")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        hdr = tk.Frame(outer, bg="#F5F5F5")
        hdr.grid(row=0, column=0, sticky="ew")
        tk.Label(hdr, text="\U0001f5fa  Map Preview", bg="#F5F5F5",
                 font=("Segoe UI", 10, "bold"), anchor="w", padx=8, pady=5).pack(side="left")

        map_inner = tk.Frame(outer, bg="white")
        map_inner.grid(row=1, column=0, sticky="nsew", padx=8, pady=6)
        map_inner.columnconfigure(0, weight=1)
        map_inner.rowconfigure(0, weight=1)

        if _MAP_AVAILABLE:
            self._map_widget = tkintermapview.TkinterMapView(
                map_inner, width=460, height=260, corner_radius=0)
            self._map_widget.grid(row=0, column=0, sticky="nsew")
            self._map_widget.set_tile_server(
                "https://a.tile.openstreetmap.org/{z}/{x}/{y}.png")
            self._map_widget.set_zoom(4)
        else:
            self._map_widget = None
            ph = tk.Canvas(map_inner, bg="#E8E8E8", highlightthickness=0)
            ph.grid(row=0, column=0, sticky="nsew")
            ph.bind("<Configure>", lambda e: (
                ph.delete("all"),
                ph.create_text(e.width // 2, e.height // 2,
                               text="Map preview requires:\npip install tkintermapview",
                               fill="#888888", font=("Segoe UI", 10), justify="center")))

        self._legend_frame = tk.Frame(outer, bg="white")
        self._legend_frame.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 2))

        arrows_row = tk.Frame(outer, bg="white")
        arrows_row.grid(row=3, column=0, sticky="ew", padx=8, pady=(0, 6))
        ttk.Checkbutton(arrows_row, text="Show direction arrows",
                        variable=self.var_show_arrows).pack(anchor="w")

    def _refresh_map(self, fit: bool = True) -> None:
        if not _MAP_AVAILABLE or self._map_widget is None:
            return

        for obj in self._map_objects:
            try:
                obj.delete()
            except Exception:
                pass
        self._map_objects.clear()
        self._pin_images.clear()
        self._arrow_images.clear()

        for w in self._legend_frame.winfo_children():
            w.destroy()

        if not self._files:
            return

        has_sel = self._selected_idx is not None
        rm_overlaps = self.var_rm_overlaps.get()
        all_lats: List[float] = []
        all_lons: List[float] = []
        prev_end_ts = 0

        for i, fi in enumerate(self._files):
            color   = _color_for(i)
            visible = self._file_visible[i] if i < len(self._file_visible) else True

            if not visible or not fi.gps_track:
                self._add_legend_entry(i, color, active=False)
                continue

            # Trim track to remove temporal overlap with previous file
            if rm_overlaps and prev_end_ts > 0:
                display_track = [pt for pt in fi.gps_track if pt[2] == 0 or pt[2] > prev_end_ts]
            else:
                display_track = fi.gps_track

            # Update boundary for next file
            if fi.end_ts:
                prev_end_ts = fi.end_ts

            if not display_track:
                self._add_legend_entry(i, color, active=True)
                continue

            # Colour / width based on selection
            if has_sel:
                draw_color = color           if i == self._selected_idx else _dim_color(color)
                draw_width = 5              if i == self._selected_idx else 2
            else:
                draw_color, draw_width = color, 3

            path_obj = self._map_widget.set_path(_coords(display_track),
                                                  color=draw_color, width=draw_width)
            self._map_objects.append(path_obj)

            all_lats.extend(pt[0] for pt in display_track)
            all_lons.extend(pt[1] for pt in display_track)

            # Start marker
            slat, slon, _ = display_track[0]
            if _PIL_AVAILABLE:
                pin = _make_circle_marker(color, i + 1, size=20)
                self._pin_images.append(pin)
                m = self._map_widget.set_marker(slat, slon, icon=pin, icon_anchor="center")
            else:
                m = self._map_widget.set_marker(
                    slat, slon, text=str(i + 1),
                    marker_color_circle=color, marker_color_outside=color,
                    text_color="white", font=("Segoe UI", 8))
            self._map_objects.append(m)

            # Directional arrows
            if self.var_show_arrows.get():
                for alat, alon, abearing in _sample_arrows(display_track):
                    if _PIL_AVAILABLE:
                        arr = _make_arrow_marker(draw_color, abearing, size=13)
                        self._arrow_images.append(arr)
                        am = self._map_widget.set_marker(alat, alon,
                                                         icon=arr, icon_anchor="center")
                    else:
                        am = self._map_widget.set_marker(
                            alat, alon, text="\u25b2",
                            marker_color_circle=draw_color,
                            marker_color_outside=draw_color,
                            text_color="white", font=("Segoe UI", 7))
                    self._map_objects.append(am)

            self._add_legend_entry(i, color, active=True)

        if all_lats and fit:
            min_lat, max_lat = min(all_lats), max(all_lats)
            min_lon, max_lon = min(all_lons), max(all_lons)
            try:
                self._map_widget.fit_bounding_box((max_lat, min_lon), (min_lat, max_lon))
            except Exception:
                clat = (min_lat + max_lat) / 2
                clon = (min_lon + max_lon) / 2
                span = max(max_lat - min_lat, max_lon - min_lon)
                zoom = max(1, min(17, int(math.log2(360 / max(span, 0.001)))))
                self._map_widget.set_position(clat, clon)
                self._map_widget.set_zoom(zoom)

    def _add_legend_entry(self, idx: int, color: str, active: bool) -> None:
        entry = tk.Frame(self._legend_frame, bg="white")
        entry.pack(side="left", padx=(0, 12))
        lc = tk.Canvas(entry, width=30, height=14, bg="white", highlightthickness=0)
        lc.pack(side="left")
        lc.create_line(2, 7, 28, 7, fill=color if active else "#CCCCCC", width=3)
        tk.Label(entry, text=f"Activity {idx + 1}", bg="white",
                 font=("Segoe UI", 9),
                 fg="#444444" if active else "#AAAAAA").pack(side="left")

    # ── Options section ────────────────────────────────────────────────────────

    def _build_options_section(self, parent: tk.Frame) -> None:
        self._opts_outer = tk.Frame(parent, bg="white",
                                    highlightbackground="#D0D0D0", highlightthickness=1)
        self._opts_outer.pack(fill="x", pady=(0, 6))

        hdr = tk.Frame(self._opts_outer, bg="#F5F5F5", cursor="hand2")
        hdr.pack(fill="x")
        tk.Label(hdr, text="\u2699  Merge Options", bg="#F5F5F5",
                 font=("Segoe UI", 10, "bold"), anchor="w", padx=8, pady=5).pack(side="left")
        self._lbl_toggle = tk.Label(hdr, text="\u25b2", bg="#F5F5F5",
                                     font=("Segoe UI", 9), padx=8)
        self._lbl_toggle.pack(side="right")
        for w in (hdr, *hdr.winfo_children()):
            w.bind("<Button-1>", self._toggle_options)

        self._opts_content = tk.Frame(self._opts_outer, bg="white", padx=12, pady=6)
        self._opts_content.pack(fill="x")
        ttk.Checkbutton(self._opts_content, text="Remove overlaps",
                         variable=self.var_rm_overlaps).pack(anchor="w", pady=2)

    def _toggle_options(self, _event: Any = None) -> None:
        self._options_open = not self._options_open
        if self._options_open:
            self._opts_content.pack(fill="x")
            self._lbl_toggle.config(text="\u25b2")
        else:
            self._opts_content.pack_forget()
            self._lbl_toggle.config(text="\u25be")

    # ── Output section ─────────────────────────────────────────────────────────

    def _build_output_section(self, parent: tk.Frame) -> None:
        outer = tk.Frame(parent, bg="white",
                         highlightbackground="#D0D0D0", highlightthickness=1)
        outer.pack(fill="x", pady=(0, 6))
        inner = tk.Frame(outer, bg="white", padx=8, pady=8)
        inner.pack(fill="x")
        inner.columnconfigure(1, weight=1)
        tk.Label(inner, text="Output file:", bg="white",
                 font=("Segoe UI", 9), anchor="w").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(inner, textvariable=self.var_output,
                  state="readonly").grid(row=0, column=1, sticky="ew")
        ttk.Button(inner, text="Browse\u2026",
                   command=self._browse_output).grid(row=0, column=2, padx=(6, 0))

    # ── Merge button ───────────────────────────────────────────────────────────

    def _build_merge_button(self, parent: tk.Frame) -> None:
        frame = tk.Frame(parent, bg="#EBEBEB")
        frame.pack(fill="x", pady=(0, 6))
        self.btn_merge = tk.Button(
            frame, text="Merge into single activity",
            command=self._on_merge, state="disabled",
            bg="#9EAFC2", fg="white", font=("Segoe UI", 11, "bold"),
            relief="flat", padx=20, pady=10, cursor="hand2",
            activebackground="#1E5D94", activeforeground="white")
        self.btn_merge.pack(fill="x")

    # ── Result section ─────────────────────────────────────────────────────────

    def _build_result_section(self, parent: tk.Frame) -> None:
        self._result_outer = tk.Frame(parent, bg="white",
                                       highlightbackground="#D0D0D0", highlightthickness=1)
        self._result_outer.pack(fill="x", pady=(0, 6))   # always visible

        hdr = tk.Frame(self._result_outer, bg="#F5F5F5")
        hdr.pack(fill="x")
        tk.Label(hdr, text="\U0001f4ca  Result", bg="#F5F5F5",
                 font=("Segoe UI", 10, "bold"), anchor="w", padx=8, pady=5).pack(side="left")

        # Placeholder shown when no files are loaded
        self._result_placeholder = tk.Label(
            self._result_outer,
            text="Add at least 2 files to preview the merged result",
            bg="white", fg="#AAAAAA", font=("Segoe UI", 9), pady=10)
        self._result_placeholder.pack()

        # Stats grid (hidden until files are loaded)
        self._result_content = tk.Frame(self._result_outer, bg="white", padx=12, pady=8)
        self._result_content.columnconfigure(0, weight=1)
        self._result_content.columnconfigure(1, weight=1)

        bf = tk.Frame(self._result_content, bg="white")
        bf.grid(row=0, column=0, sticky="nw")
        tk.Label(bf, text="Before:", bg="white", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.lbl_before_count = tk.Label(bf, text="", bg="white", font=("Segoe UI", 9))
        self.lbl_before_dist  = tk.Label(bf, text="", bg="white", font=("Segoe UI", 9))
        self.lbl_before_count.pack(anchor="w")
        self.lbl_before_dist.pack(anchor="w")

        af = tk.Frame(self._result_content, bg="white")
        af.grid(row=0, column=1, sticky="nw", padx=(12, 0))
        tk.Label(af, text="After:", bg="white", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.lbl_after_dist = tk.Label(af, text="", bg="white",
                                        font=("Segoe UI", 9, "bold"), fg="#2E6DA4")
        self.lbl_after_time = tk.Label(af, text="", bg="white", font=("Segoe UI", 9))
        self.lbl_after_pace = tk.Label(af, text="", bg="white",
                                        font=("Segoe UI", 9), fg="#666666")
        self.lbl_after_dist.pack(anchor="w")
        self.lbl_after_time.pack(anchor="w")
        self.lbl_after_pace.pack(anchor="w")

        wf = tk.Frame(self._result_outer, bg="white")
        wf.pack(fill="x", padx=12, pady=(0, 8))
        self.lbl_written = tk.Label(wf, text="", bg="white",
                                     font=("Segoe UI", 9, "bold"), fg="#27AE60")
        self.lbl_written.pack(anchor="center")

    # ── Live result preview ────────────────────────────────────────────────────

    def _refresh_results(self) -> None:
        """Recompute and display result stats from current file list."""
        if len(self._files) < 2:
            self._result_content.pack_forget()
            self._result_placeholder.pack()
            self.lbl_written.config(text="")
            return

        self._result_placeholder.pack_forget()
        self._result_content.pack(fill="x")

        total_d = sum(fi.distance_km for fi in self._files)
        total_t = sum(fi.timer_ms    for fi in self._files)

        self.lbl_before_count.config(text=f"\u2022 {len(self._files)} activities")
        self.lbl_before_dist.config( text=f"\u2022 {total_d:.2f} km total")
        self.lbl_after_dist.config(  text=f"\u25cf {total_d:.2f} km")
        self.lbl_after_time.config(  text=f"  {fmt_time(total_t)}")

        if total_d > 0 and total_t > 0:
            pace_s = (total_t / 1000) / total_d
            self.lbl_after_pace.config(
                text=f"Avg pace: {int(pace_s // 60)}:{int(pace_s % 60):02d} /km")
        else:
            self.lbl_after_pace.config(text="")

        # Clear the written confirmation — it's stale if the file list changed
        self.lbl_written.config(text="")

    # ── File management ────────────────────────────────────────────────────────

    def _add_paths(self, paths: List[str]) -> None:
        existing = {fi.path for fi in self._files}
        added = 0
        for raw_path in paths:
            path = os.path.normpath(raw_path)
            if not path.lower().endswith(".fit") or not os.path.isfile(path):
                continue
            if path in existing:
                continue
            try:
                fi = _load_file_info(path)
            except (OSError, ValueError):
                continue
            self._files.append(fi)
            self._file_visible.append(True)
            existing.add(path)
            added += 1

        if added:
            result = _zip_sort(self._files, self._file_visible, key=lambda fi: fi.ts)
            self._files, self._file_visible = result[0], result[1]
            self._refresh_files_ui()
            self._refresh_map()
            self._refresh_results()
            self._update_merge_button()

    def _add_files(self) -> None:
        last_dir = _dir_of(self._files[-1].path if self._files else "")
        paths = filedialog.askopenfilenames(title="Select FIT files",
                                             filetypes=FIT_FILETYPES, initialdir=last_dir)
        if paths:
            self._add_paths(list(paths))

    def _remove_file(self, idx: int) -> None:
        if not (0 <= idx < len(self._files)):
            return
        del self._files[idx]
        del self._file_visible[idx]
        if self._selected_idx == idx:
            self._selected_idx = None
        elif self._selected_idx is not None and self._selected_idx > idx:
            self._selected_idx -= 1
        self._refresh_files_ui()
        self._refresh_map()
        self._refresh_results()
        self._update_merge_button()

    def _clear_files(self) -> None:
        self._files.clear()
        self._file_visible.clear()
        self._selected_idx = None
        self._refresh_files_ui()
        self._refresh_map()
        self._refresh_results()
        self._update_merge_button()

    def _move_file(self, idx: int, direction: int) -> None:
        new_idx = idx + direction
        if 0 <= new_idx < len(self._files):
            self._files[idx],        self._files[new_idx]        = self._files[new_idx],        self._files[idx]
            self._file_visible[idx], self._file_visible[new_idx] = self._file_visible[new_idx], self._file_visible[idx]
            if   self._selected_idx == idx:     self._selected_idx = new_idx
            elif self._selected_idx == new_idx: self._selected_idx = idx
            self._refresh_files_ui()
            self._refresh_map(fit=False)

    def _sort_by_time(self) -> None:
        result = _zip_sort(self._files, self._file_visible, key=lambda fi: fi.ts)
        self._files, self._file_visible = result[0], result[1]
        self._selected_idx = None
        self._refresh_files_ui()
        self._refresh_map(fit=False)

    def _toggle_visibility(self, idx: int) -> None:
        if 0 <= idx < len(self._file_visible):
            self._file_visible[idx] = not self._file_visible[idx]
            self._refresh_files_ui()
            self._refresh_map(fit=False)

    def _select_file(self, idx: int) -> None:
        self._selected_idx = None if self._selected_idx == idx else idx
        self._refresh_files_ui()
        self._refresh_map(fit=False)

    def _on_drop(self, event: Any) -> None:
        self._add_paths(_parse_dnd_paths(event.data))

    # ── Output ─────────────────────────────────────────────────────────────────

    def _browse_output(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save merged FIT file as", filetypes=FIT_FILETYPES,
            defaultextension=".fit", initialdir=_dir_of(self.var_output.get()))
        if path:
            self.var_output.set(path)

    # ── Merge ──────────────────────────────────────────────────────────────────

    def _update_merge_button(self, *_: Any) -> None:
        ready = len(self._files) >= 2 and bool(self.var_output.get())
        self.btn_merge.config(state="normal" if ready else "disabled",
                               bg="#2E6DA4" if ready else "#9EAFC2")

    def _on_merge(self) -> None:
        out = self.var_output.get()
        for fi in self._files:
            if not os.path.isfile(fi.path):
                messagebox.showerror("Error", f"File not found:\n{fi.path}")
                return
        out_abs = os.path.abspath(out)
        if any(os.path.abspath(fi.path) == out_abs for fi in self._files):
            messagebox.showerror("Error", "Output path must differ from all input paths.")
            return

        self.btn_merge.config(state="disabled", text="Merging\u2026")
        self.update_idletasks()

        try:
            datasets = [open(fi.path, "rb").read() for fi in self._files]
            result   = merge_all(datasets, remove_overlaps=self.var_rm_overlaps.get())
        except Exception as exc:
            messagebox.showerror("Merge Failed", str(exc))
            self.btn_merge.config(state="normal", text="Merge into single activity", bg="#2E6DA4")
            return

        try:
            with open(out, "wb") as fh:
                fh.write(result)
        except OSError as exc:
            messagebox.showerror("Write Failed", str(exc))
            self.btn_merge.config(state="normal", text="Merge into single activity", bg="#2E6DA4")
            return

        self.lbl_written.config(text=f"\u2705  Written: {os.path.basename(out)}")
        self.btn_merge.config(state="normal", text="Merge into single activity", bg="#2E6DA4")
