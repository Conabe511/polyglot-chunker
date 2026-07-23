#!/usr/bin/env python3
"""
video_to_polyglot_chunks.py

Converts an MP4 or MKV into a folder of PNG/MPEG-TS polyglot files.

FOLDER STRUCTURE PRODUCED
--------------------------
  Normal mode:
    <output_dir>/
      raw/
        chunks/                   ← two layers deep under output_dir
          chunk_000.ts
          ...
      polyglot/
        chunk_000.png             ← each is a valid PNG *and* a valid MPEG-TS
        ...
      manifest.json
      manifest.m3u8

  --hidden mode:
    <output_dir>/
      <random1>.png             ← randomized .png polyglots directly here
      <random2>.png
      ...
      <random_m3u8>.m3u8        ← HLS playlist (no manifest.json, no subfolders)

REQUIREMENTS
------------
  - Python 3.8+
  - FFmpeg + ffprobe in PATH
  - png_ts_polyglot.py in the same directory as this script (or on PYTHONPATH)

USAGE
-----
  python video_to_polyglot_chunks.py input.mp4 cover.png output_dir/
  python video_to_polyglot_chunks.py input.mkv cover.png out/ --chunk-duration 5 --pad
  python video_to_polyglot_chunks.py input.mp4 cover.png out/ --keep-raw
  python video_to_polyglot_chunks.py input.mp4 cover.png out/ --hidden

  --chunk-duration N  Segment length in seconds (default: 10)
  --pad               Align each TS payload to a packet-size boundary inside
                      the polyglot file (see png_ts_polyglot.py for details)
  --keep-raw          Keep the raw .ts chunks after polyglots are built
                      (they are deleted by default to save space)
  --hidden            Hidden mode: randomized PNG + m3u8 files directly in output_dir
                      (no /polyglot/, no manifest.json)
  --jobs N            Number of parallel polyglot workers (default: CPU count)
  --verbose           Print per-chunk FFmpeg and polyglot output
"""

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Locate png_ts_polyglot alongside this script
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

try:
    from png_ts_polyglot import build_polyglot, find_png_end, validate_ts
except ModuleNotFoundError as exc:
    sys.exit(
        f"[x] Cannot import png_ts_polyglot: {exc}\n"
        "    Make sure png_ts_polyglot.py is in the same directory as this script."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def check_ffmpeg() -> tuple[str, str]:
    """Return (ffmpeg_path, ffprobe_path) or exit with a helpful message."""
    ffmpeg  = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    missing = [name for name, p in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not p]
    if missing:
        sys.exit(f"[x] Required tool(s) not found in PATH: {', '.join(missing)}")
    return ffmpeg, ffprobe  # type: ignore[return-value]


def probe_video(ffprobe: str, path: Path) -> dict:
    """
    Return a dict with duration, codec names, and raw ffprobe output.
    Exits on probe failure.
    """
    cmd = [
        ffprobe, "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-show_format",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as exc:
        sys.exit(f"[x] ffprobe failed on {path}:\n{exc.stderr}")

    info = json.loads(result.stdout)
    fmt  = info.get("format", {})
    streams = info.get("streams", [])

    duration = float(fmt.get("duration", 0))
    video_codecs = [
        s.get("codec_name", "unknown")
        for s in streams if s.get("codec_type") == "video"
    ]
    audio_codecs = [
        s.get("codec_name", "unknown")
        for s in streams if s.get("codec_type") == "audio"
    ]
    return {
        "duration":     duration,
        "format_name":  fmt.get("format_name", ""),
        "bit_rate":     int(fmt.get("bit_rate", 0)),
        "video_codecs": video_codecs,
        "audio_codecs": audio_codecs,
        "nb_streams":   len(streams),
    }


def segment_video(ffmpeg: str, input_path: Path, chunks_dir: Path,
                  chunk_duration: int, verbose: bool) -> list[Path]:
    """
    Use FFmpeg's segment muxer to cut the input into .ts files inside
    `chunks_dir`. Returns the sorted list of produced chunk paths.
    """
    pattern = str(chunks_dir / "chunk_%05d.ts")
    cmd = [
        ffmpeg,
        "-y",                         # overwrite without asking
        "-i", str(input_path),
        "-c", "copy",                 # stream-copy: no re-encode
        "-f", "segment",
        "-segment_time", str(chunk_duration),
        "-segment_format", "mpegts",
        # NOTE: do NOT pass -reset_timestamps 1 here.
        # Resetting PTS to 0 at each segment boundary breaks HLS stitching:
        # the player expects monotonically increasing timestamps across
        # segments and treats a backwards jump as stream corruption.
        # Without the flag, the segment muxer carries timestamps forward
        # continuously, which is what EXT-X-BYTERANGE HLS playback needs.
        pattern,
    ]

    print(f"[>] Segmenting {input_path.name} into {chunk_duration}s chunks …")
    run_kwargs: dict = dict(
        check=True,
        text=True,
    )
    if not verbose:
        run_kwargs["capture_output"] = True

    try:
        subprocess.run(cmd, **run_kwargs)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr if exc.stderr else "(no stderr captured)"
        sys.exit(f"[x] FFmpeg segmentation failed:\n{detail}")

    chunks = sorted(chunks_dir.glob("chunk_*.ts"))
    if not chunks:
        sys.exit("[x] FFmpeg ran without error but produced no .ts files. "
                 "Check that the input file has a readable video/audio stream.")
    print(f"[+] {len(chunks)} chunk(s) written to {chunks_dir}")
    return chunks


def probe_chunk(ffprobe: str, chunk_path: Path) -> dict:
    """Return {duration, start_time, size_bytes} for a single TS chunk."""
    cmd = [
        ffprobe, "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        str(chunk_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        fmt = json.loads(result.stdout).get("format", {})
        return {
            "duration":   float(fmt.get("duration", 0)),
            "start_time": float(fmt.get("start_time", 0)),
            "size_bytes": chunk_path.stat().st_size,
        }
    except Exception:
        # Chunk metadata is best-effort; don't abort the whole run
        return {
            "duration":   None,
            "start_time": None,
            "size_bytes": chunk_path.stat().st_size,
        }


def process_chunk(
    index: int,
    chunk_path: Path,
    png_path: Path,
    polyglot_dir: Path,
    ffprobe: str,
    pad: bool,
    verbose: bool,
    hidden_mode: bool = False,
) -> dict:
    """
    Build one polyglot file and return its entry for the manifest.
    Runs in a thread-pool worker.
    """
    if hidden_mode:
        # Randomized filename for hidden mode (secure)
        out_name = f"{secrets.token_hex(12)}.png"
    else:
        out_name = chunk_path.stem + ".png"
    out_path     = polyglot_dir / out_name
    chunk_meta   = probe_chunk(ffprobe, chunk_path)

    try:
        poly_meta = build_polyglot(
            png_path=png_path,
            ts_path=chunk_path,
            output_path=out_path,
            pad_to_packet_boundary=pad,
            quiet=not verbose,
        )
        status = "ok"
        error  = None
    except Exception as exc:
        poly_meta = {}
        status    = "error"
        error     = str(exc)
        print(f"  [!] chunk {index:05d}: {exc}", file=sys.stderr)

    return {
        "index":        index,
        "chunk_name":   chunk_path.name,
        "raw_path":     str(chunk_path.relative_to(chunk_path.parent.parent.parent)),
        "polyglot_path": str(out_path.relative_to(polyglot_dir)) if status == "ok" else None,
        "polyglot_name": out_name if status == "ok" else None,
        "status":       status,
        "error":        error,
        # Chunk timing (from ffprobe on the raw .ts)
        "start_time_s": chunk_meta["start_time"],
        "duration_s":   chunk_meta["duration"],
        # File sizes
        "raw_size_bytes":      chunk_meta["size_bytes"],
        "polyglot_size_bytes": poly_meta.get("output_size"),
        # Polyglot layout detail
        "png_size_bytes":     poly_meta.get("png_size"),
        "padding_bytes":      poly_meta.get("padding_size"),
        "ts_offset_bytes":    poly_meta.get("ts_offset"),
        "ts_packet_size":     poly_meta.get("packet_size"),
    }


def build_manifest(
    input_path: Path,
    png_path: Path,
    output_dir: Path,
    video_info: dict,
    chunk_entries: list[dict],
    chunk_duration: int,
    pad: bool,
    elapsed_s: float,
) -> dict:
    ok     = [e for e in chunk_entries if e["status"] == "ok"]
    failed = [e for e in chunk_entries if e["status"] != "ok"]

    return {
        "schema_version": 1,
        "created_utc":   datetime.now(timezone.utc).isoformat(),
        "generator":     "video_to_polyglot_chunks.py",
        "input": {
            "path":         str(input_path.resolve()),
            "filename":     input_path.name,
            "size_bytes":   input_path.stat().st_size,
            "duration_s":   video_info["duration"],
            "format":       video_info["format_name"],
            "bit_rate":     video_info["bit_rate"],
            "video_codecs": video_info["video_codecs"],
            "audio_codecs": video_info["audio_codecs"],
        },
        "png": {
            "path":       str(png_path.resolve()),
            "filename":   png_path.name,
            "size_bytes": png_path.stat().st_size,
        },
        "settings": {
            "chunk_duration_s":   chunk_duration,
            "pad_to_ts_boundary": pad,
        },
        "output_dir": str(output_dir.resolve()),
        "summary": {
            "total_chunks":     len(chunk_entries),
            "successful":       len(ok),
            "failed":           len(failed),
            "total_raw_bytes":  sum(e["raw_size_bytes"] or 0 for e in chunk_entries),
            "total_polyglot_bytes": sum(e["polyglot_size_bytes"] or 0 for e in ok),
            "elapsed_s":        round(elapsed_s, 2),
        },
        "chunks": chunk_entries,
    }


def write_m3u8(m3u8_path: Path, manifest: dict, hidden_mode: bool = False) -> None:
    """
    Write an HLS v4 playlist alongside the JSON manifest.

    WHY THIS FIXES VLC
    ------------------
    Polyglot files have a .png extension, so VLC (and most players) open
    them with the image decoder rather than the TS demuxer — the video is
    never even attempted.  HLS version 4 supports EXT-X-BYTERANGE, which
    tells the player to read only a specific slice of bytes from each file.
    We point that slice exactly at the TS payload (after the PNG header and
    any alignment padding), so the player feeds raw MPEG-TS packets straight
    to the demuxer without ever touching the PNG prefix.  VLC's HLS engine
    handles byte-range segments correctly and works around the extension
    ambiguity entirely.

    PATHS
    -----
    All segment paths are relative to this playlist file (in output_dir/).
    In normal mode: polyglot/ prefix. In --hidden: direct filenames (no prefix).
    """
    ok_chunks = [c for c in manifest["chunks"] if c.get("status") == "ok"]
    if not ok_chunks:
        return

    # TARGETDURATION must be >= every segment duration (HLS spec §4.3.3.1)
    max_dur = max(
        (c["duration_s"] or manifest["settings"]["chunk_duration_s"])
        for c in ok_chunks
    )
    target_duration = int(max_dur) + 1   # ceil-ish; +1 is safe and common

    prefix = "" if hidden_mode else "polyglot/"

    lines: list[str] = [
        "#EXTM3U",
        "#EXT-X-VERSION:4",              # minimum version that supports BYTERANGE
        f"#EXT-X-TARGETDURATION:{target_duration}",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "",
    ]

    for chunk in ok_chunks:
        dur      = chunk["duration_s"] or manifest["settings"]["chunk_duration_s"]
        length   = chunk["raw_size_bytes"]    # TS payload byte count
        offset   = chunk["ts_offset_bytes"]   # byte offset of TS inside the polyglot
        filename = chunk["polyglot_name"]     # e.g. chunk_00000.png

        lines.append(f"#EXTINF:{dur:.6f},")
        lines.append(f"#EXT-X-BYTERANGE:{length}@{offset}")
        lines.append(f"{prefix}{filename}")
        lines.append("")

    lines.append("#EXT-X-ENDLIST")
    m3u8_path.write_text("\n".join(lines) + "\n")


def print_summary(manifest: dict) -> None:
    s  = manifest["summary"]
    ok = s["successful"]
    n  = s["total_chunks"]
    mb = lambda b: f"{b / 1_048_576:.1f} MB"

    print()
    print("╔══════════════════════════════════════════╗")
    print("║          polyglot chunk summary          ║")
    print("╠══════════════════════════════════════════╣")
    print(f"║  chunks built:  {ok}/{n:<24}║")
    if s["failed"]:
        print(f"║  FAILED:        {s['failed']:<25}║")
    print(f"║  raw total:     {mb(s['total_raw_bytes']):<25}║")
    print(f"║  polyglot total:{mb(s['total_polyglot_bytes']):<25}║")
    print(f"║  elapsed:       {s['elapsed_s']:.1f}s{'':<22}║")
    print("╚══════════════════════════════════════════╝")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert an MP4/MKV to a folder of PNG+MPEG-TS polyglot chunks.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input",    type=Path, help="Input .mp4 or .mkv file")
    parser.add_argument("png",      type=Path, help="PNG image to embed in every chunk")
    parser.add_argument("output",   type=Path, help="Output directory (created if absent)")
    parser.add_argument(
        "--chunk-duration", type=int, default=10, metavar="N",
        help="Segment length in seconds (default: 10)",
    )
    parser.add_argument(
        "--pad", action="store_true",
        help="Pad each polyglot so the TS payload starts on a packet-size boundary",
    )
    parser.add_argument(
        "--keep-raw", action="store_true",
        help="Keep raw .ts chunks after polyglots are built (deleted by default)",
    )
    parser.add_argument(
        "--jobs", type=int, default=os.cpu_count() or 4, metavar="N",
        help="Parallel polyglot workers (default: CPU count)",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Show per-chunk FFmpeg and polyglot output",
    )
    parser.add_argument(
        "--hidden", action="store_true",
        help="Hidden mode: randomized PNG filenames + m3u8 directly in output_dir "
             "(no polyglot/ subdir, no manifest.json)",
    )
    args = parser.parse_args()

    # ── Input validation ────────────────────────────────────────────────────
    if not args.input.is_file():
        sys.exit(f"[x] Input file not found: {args.input}")
    if args.input.suffix.lower() not in (".mp4", ".mkv"):
        sys.exit(f"[x] Input must be .mp4 or .mkv, got: {args.input.suffix!r}")
    if not args.png.is_file():
        sys.exit(f"[x] PNG file not found: {args.png}")
    if args.chunk_duration < 1:
        sys.exit("[x] --chunk-duration must be ≥ 1")

    # Quick PNG sanity-check (find_png_end raises ValueError on bad input)
    try:
        find_png_end(args.png.read_bytes())
    except ValueError as exc:
        sys.exit(f"[x] PNG validation failed: {exc}")

    ffmpeg, ffprobe = check_ffmpeg()

    # ── Directory layout ────────────────────────────────────────────────────
    output_dir   = args.output.resolve()
    chunks_dir   = output_dir / "raw" / "chunks"     # two layers under output_dir
    hidden_mode  = getattr(args, 'hidden', False)
    if hidden_mode:
        polyglot_dir = output_dir  # PNGs go directly here
        m3u8_name = f"playlist_{secrets.token_hex(8)}.m3u8"
        m3u8_path = output_dir / m3u8_name
        manifest_path = None
    else:
        polyglot_dir = output_dir / "polyglot"
        manifest_path = output_dir / "manifest.json"
        m3u8_path     = output_dir / "manifest.m3u8"

    chunks_dir.mkdir(parents=True, exist_ok=True)
    if not hidden_mode:
        polyglot_dir.mkdir(parents=True, exist_ok=True)

    # ── Probe source ────────────────────────────────────────────────────────
    print(f"[>] Probing {args.input.name} …")
    video_info = probe_video(ffprobe, args.input)
    dur = video_info["duration"]
    expected = int(dur / args.chunk_duration) + 1
    print(f"    Duration: {dur:.1f}s  |  ~{expected} chunk(s) at {args.chunk_duration}s each")
    print(f"    Video: {', '.join(video_info['video_codecs']) or 'none'}")
    print(f"    Audio: {', '.join(video_info['audio_codecs']) or 'none'}")

    # ── Segment ─────────────────────────────────────────────────────────────
    t_start = time.monotonic()
    chunks  = segment_video(ffmpeg, args.input, chunks_dir,
                            args.chunk_duration, args.verbose)

    # ── Polyglot pass (parallel) ────────────────────────────────────────────
    print(f"[>] Building polyglots with {args.jobs} worker(s) …")
    results: list[dict] = [{}] * len(chunks)

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {
            pool.submit(
                process_chunk,
                idx, chunk, args.png, polyglot_dir, ffprobe, args.pad, args.verbose, hidden_mode
            ): idx
            for idx, chunk in enumerate(chunks)
        }
        done = 0
        for future in as_completed(futures):
            idx = futures[future]
            try:
                results[idx] = future.result()
            except Exception as exc:
                results[idx] = {
                    "index":  idx,
                    "status": "error",
                    "error":  str(exc),
                    "chunk_name": chunks[idx].name,
                }
            done += 1
            if not args.verbose:
                pct = done / len(chunks) * 100
                bar = "█" * int(pct / 4) + "░" * (25 - int(pct / 4))
                print(f"\r    [{bar}] {done}/{len(chunks)}", end="", flush=True)

    if not args.verbose:
        print()  # newline after progress bar

    elapsed = time.monotonic() - t_start

    # ── Manifest & Playlist ──────────────────────────────────────────────────
    manifest = build_manifest(
        input_path=args.input,
        png_path=args.png,
        output_dir=output_dir,
        video_info=video_info,
        chunk_entries=results,
        chunk_duration=args.chunk_duration,
        pad=args.pad,
        elapsed_s=elapsed,
    )
    if not hidden_mode:
        manifest_path.write_text(json.dumps(manifest, indent=2))
        print(f"[+] Manifest written:  {manifest_path}")
        write_m3u8(m3u8_path, manifest, hidden_mode=False)
        print(f"[+] Playlist written:  {m3u8_path}")
    else:
        write_m3u8(m3u8_path, manifest, hidden_mode=True)
        print(f"[+] Hidden playlist written:  {m3u8_path}")

    # ── Clean up raw chunks ──────────────────────────────────────────────────
    if not args.keep_raw:
        shutil.rmtree(output_dir / "raw")
        print("[+] Raw .ts chunks removed (use --keep-raw to retain them)")
    else:
        raw_size = sum(c.stat().st_size for c in chunks)
        print(f"[+] Raw chunks kept in {output_dir / 'raw'} ({raw_size / 1_048_576:.1f} MB)")

    print_summary(manifest)

    failed = [e for e in results if e.get("status") != "ok"]
    if failed:
        if hidden_mode:
            print(f"[!] {len(failed)} chunk(s) failed — check output files",
                  file=sys.stderr)
        else:
            print(f"[!] {len(failed)} chunk(s) failed — see manifest.json for details",
                  file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()