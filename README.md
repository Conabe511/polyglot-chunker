# the chunker

Tools for building PNG/MPEG-TS **polyglot files** — files that are simultaneously a valid PNG image and a valid MPEG-TS stream — and for chunking a video into a folder of such polyglots with an accompanying HLS playlist.

## How the polyglot trick works

- PNG decoders parse chunks starting at the 8-byte PNG signature and stop as soon as they hit the `IEND` chunk. Anything appended after `IEND` is ignored by well-behaved PNG decoders.
- MPEG-TS demuxers (ffmpeg, VLC, etc.) don't require the `0x47` sync byte to sit at file offset 0 — they scan forward for a byte that repeats every 188 bytes and lock onto that as the start of the stream.

So a polyglot is built by concatenating a valid, trimmed PNG with a valid TS file: `[PNG bytes][TS bytes]`.

## Scripts

### `png_ts_polyglot.py`

Builds a single PNG/TS polyglot from a source PNG and a source `.ts` file.

```
python png_ts_polyglot.py cover.png segment.ts output.png [--pad]
```

- Validates the PNG (finds and trims to the true end of the `IEND` chunk).
- Validates the TS file (confirms the `0x47` sync byte recurs every 188 or 204 bytes).
- Concatenates them into `output.png`.
- `--pad`: pads after `IEND` so the TS payload starts on a packet-size-aligned byte offset.

Can also be imported and used programmatically via `build_polyglot()`, `find_png_end()`, and `validate_ts()`.

**Caveats:**
- Not guaranteed to work with every player — some TS demuxers only probe a limited number of bytes at the start of a file.
- Naive/embedded TS players that assume packets start at file offset 0 (no scanning) will not recognize the file as TS.

### `video_to_polyglot_chunks.py`

Converts an `.mp4`/`.mkv` into a folder of PNG/TS polyglot chunks plus an HLS playlist, using FFmpeg to segment the video (stream-copy, no re-encode).

```
python video_to_polyglot_chunks.py input.mp4 cover.png output_dir/
```

**Options:**

| Flag | Description |
|---|---|
| `--chunk-duration N` | Segment length in seconds (default: 10) |
| `--pad` | Align each TS payload to a packet-size boundary inside the polyglot |
| `--keep-raw` | Keep the raw `.ts` chunks after polyglots are built (deleted by default) |
| `--hidden` | Randomized `.png` filenames + `.m3u8` written directly into `output_dir` (no `polyglot/` subfolder, no `manifest.json`) |
| `--jobs N` | Parallel polyglot workers (default: CPU count) |
| `--verbose` | Print per-chunk FFmpeg and polyglot output |

**Requirements:** Python 3.8+, `ffmpeg`/`ffprobe` in `PATH`, and `png_ts_polyglot.py` alongside this script.

**Output layout (normal mode):**

```
output_dir/
  polyglot/
    chunk_00000.png   ← valid PNG *and* valid MPEG-TS
    ...
  manifest.json        ← per-chunk metadata (offsets, sizes, timing)
  manifest.m3u8         ← HLS playlist (see below)
```

**Output layout (`--hidden` mode):**

```
output_dir/
  <random>.png
  <random>.png
  ...
  playlist_<random>.m3u8
```

#### The HLS playlist

Since the polyglots carry a `.png` extension, most players (including VLC) open them with the image decoder and never attempt to demux them as video. The generated `.m3u8` uses HLS v4's `EXT-X-BYTERANGE` to point each playlist entry at exactly the TS payload's byte range within the file, so a byte-range-aware HLS player reads only the TS slice and feeds it straight to the demuxer.

## `m3u8_add_base_url.py`

Rewrites every relative URI in an `.m3u8` playlist (segment lines and `URI="..."` tag attributes such as `EXT-X-KEY`/`EXT-X-MAP`) to be prefixed with a base URL. Absolute URLs are left untouched.

```
python m3u8_add_base_url.py input.m3u8 [output.m3u8] https://example.com/videos/
```

If `output.m3u8` is omitted, the input file is overwritten in place.

## Requirements

- Python 3.8+
- FFmpeg + ffprobe in `PATH` (for `video_to_polyglot_chunks.py`)
