# FIT Activity Merger

Merge two or more Garmin `.fit` activity files into one.
Files are automatically ordered by activity start time.
The core merger has no external dependencies — stdlib only.

## Requirements

- Python 3.8+
- `tkinter` (included with standard Python on Windows; on Linux install `python3-tk`)

## Installation

```bash
pip install -r requirements.txt
```

This installs the optional GUI dependencies (drag & drop and map preview).
The core CLI works without them.

## How to run

### GUI

```bash
python main.py
```

**Files section**
- Drop `.fit` files onto the drop zone, or click **Add Files…** (multi-select supported)
- Each file shows its start time, distance, and a colour-coded dot that matches its track on the map
- Use ↑ / ↓ to reorder files manually, or click **Sort by time ▼** to re-sort automatically
- A warning banner appears automatically when a spatial gap between consecutive activities is detected

**Map Preview**
- Displays each activity's GPS track in a distinct colour on an OpenStreetMap map
- Numbered markers indicate the start of each activity
- A colour legend below the map identifies each track

**Merge Options** (collapsible)
- Auto-sort by start time
- Remove overlaps
- Preserve pauses

**Output file**
- Set the destination path with **Browse…** before merging

**Merge button**
- Enabled once at least two files are loaded and an output path is chosen
- After merging the Result section shows before/after statistics and confirms the file was written

### CLI

```bash
python -m fit_merger.cli -o output.fit file1.fit file2.fit [file3.fit ...]
```

Input files are sorted by activity start time before merging, regardless of the order given on the command line.

### Installed (via pip)

```bash
pip install -e .

fit-merge -o combined.fit morning.fit afternoon.fit
fit-merge -o full_day.fit part1.fit part2.fit part3.fit
```

## Optional features

All optional dependencies are listed in `requirements.txt` and can be installed at once:

```bash
pip install -r requirements.txt
```

Or individually via extras:

| Feature | Package | Extra |
|---|---|---|
| Drag & drop | `tkinterdnd2` | `pip install "fit-merger[dnd]"` |
| Map preview | `tkintermapview` | `pip install "fit-merger[map]"` |
| Both | — | `pip install "fit-merger[gui]"` |

The app detects which packages are available at startup and enables features accordingly. No restart required after installing into the same environment.

## Project structure

```
fit_merger/
├── core/
│   ├── parser.py    # FIT file parsing
│   └── merger.py    # merge logic (merge, merge_all, get_fit_start_ts)
├── ui/
│   └── app.py       # tkinter GUI
└── cli.py           # command-line entry point
tests/               # unit tests
data/samples/        # place sample .fit files here for testing
main.py              # GUI launcher
requirements.txt     # optional GUI dependencies
```

## Running tests

```bash
pip install pytest
pytest tests/
```

Place `.fit` files in `data/samples/` to enable the integration test.
