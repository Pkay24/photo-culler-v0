# Photo Culler v0

Pick which photos from a large shoot get printed. One photo at a time, keyboard-driven,
decisions saved as you go, files copied only at export.

## Requirements

Python 3.9+ and Pillow. Nothing else.

```
pip install Pillow
```

## Run

```
python cull.py /path/to/photos
```

A browser tab opens on the first photo. `Ctrl-C` to quit — decisions are already saved.

```
--port N        preferred port (default: any free port on 127.0.0.1)
--no-browser    do not open a tab
--scan-only     scan and exit
--export DEST   export current decisions to DEST and exit
```

## Keys

| Key | Action |
|---|---|
| `←` | Reject |
| `→` | Keep |
| `↑` | Maybe |
| `↓` or `U` | Undo last decision, step back |
| `⇧←` / `⇧→` | Browse back/forward without deciding (also `,` / `.`) |
| `Space` | Skip without deciding |
| `Z` (hold) | Full-resolution original, 100% zoom at the cursor |
| `E` | Export |

Trackpad and touch swipe left/right alias `←`/`→`. The keyboard is the primary interface.

## What it does to your files

Nothing. Originals are opened read-only and are never modified, moved, renamed or
re-encoded. Nothing is written into the photo folder — not the manifest, not the proxy
cache, not the export. Export is `shutil.copy2`, and collisions get a numeric suffix
rather than an overwrite. Nothing is ever deleted.

The manifest and proxy cache live per-photo-folder under the OS app-data directory:

- macOS: `~/Library/Application Support/photo-culler/<folder>-<hash>/`
- Windows: `%LOCALAPPDATA%\photo-culler\<folder>-<hash>\`
- Linux: `$XDG_DATA_HOME`(or `~/.local/share`)`/photo-culler/<folder>-<hash>/`

Re-running on the same folder keeps every existing decision and slots new files into
chronological order, and resumes at the photo you stopped on. Once every photo has been
decided there is no "where I left off", so the set reopens at the start in review mode —
use `⇧←`/`⇧→` to reach any photo and the arrow keys to change its decision.

## Review order

EXIF capture time where available, falling back to mtime, then filename — so the day's
narrative stays intact while you choose.

## RAW files

A RAW is attached to a photo when the filename stems match in the same directory; the
viewer shows a `RAW` badge. RAWs with no matching JPEG are counted at startup and listed
in the export report, but are not shown — decoding them needs a compiled dependency.

## Export

Prompted for a destination, which must be outside the photo folder.

```
DEST/
├── keep/            keeps
│   └── raw/         their attached RAWs
├── maybe/           maybes
│   └── raw/         their attached RAWs
└── export-report.json
```

Rejects and undecided shots are listed in the report but not copied.

## Tests

```
python test_cull.py
```

Covers SHA-256 identity of exported files, source-folder immutability, decision
persistence across rescan, undo, pairing and unpaired RAWs, and collision suffixing.
