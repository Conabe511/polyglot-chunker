#!/usr/bin/env python3
"""
png_ts_polyglot.py

Builds a PNG/MPEG-TS polyglot file: a file that is simultaneously a valid
PNG image AND a valid MPEG-TS stream, depending on which kind of program
reads it.

HOW IT WORKS
------------
- PNG readers parse chunks starting at the 8-byte PNG signature and stop
  as soon as they hit the IEND chunk. Anything appended after IEND is
  ignored by (well-behaved) PNG decoders.
- MPEG-TS demuxers (ffmpeg, VLC, etc.) don't require the 0x47 sync byte
  to sit at file offset 0. They scan forward through the stream looking
  for a byte that repeats every 188 bytes (the TS packet size) for a run
  of several packets, then lock onto that as the start of the stream.

So the strategy is:
  1. Validate the PNG and trim it to end exactly at IEND (dropping any
     trailing junk that might already be in the file).
  2. Validate the TS file (confirm 0x47 sync bytes recur every 188 bytes,
     the standard packet size).
  3. Concatenate: [valid PNG bytes][valid TS bytes] -> output file.

CAVEATS (please read)
----------------------
- This is a best-effort polyglot, not a guarantee for every possible
  player. Some TS demuxers only probe a limited number of bytes at the
  start of the file (ffmpeg's default probe size is generous, but very
  small probe sizes or unusual players may fail to find the TS data if
  the PNG portion is very large).
- A minority of naive/embedded TS players assume packets start at file
  offset 0 with no scanning at all. Those will NOT recognize this file
  as TS, because the file legitimately starts with PNG bytes. There's
  no way around this while also keeping the PNG signature at offset 0
  (PNG absolutely requires its 8-byte magic at the very start).
- This script does not modify or repackage the TS content itself, so
  the TS stream's internal validity is preserved byte-for-byte.
"""

import argparse
import struct
import sys
from pathlib import Path

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
TS_SYNC_BYTE = 0x47
TS_PACKET_SIZE = 188  # standard; some variants use 204 (188 + 16 FEC bytes)


def find_png_end(data: bytes) -> int:
    """
    Parse PNG chunks starting after the signature and return the byte
    offset immediately after the IEND chunk (i.e. the true end of a
    minimal valid PNG). Raises ValueError if the file isn't a well
    formed PNG or has no IEND chunk.
    """
    if data[:8] != PNG_SIGNATURE:
        raise ValueError("Not a valid PNG: missing PNG signature")

    offset = 8
    length_of_data = len(data)

    while offset + 8 <= length_of_data:
        chunk_len = struct.unpack(">I", data[offset:offset + 4])[0]
        chunk_type = data[offset + 4:offset + 8]

        chunk_total_size = 4 + 4 + chunk_len + 4  # len + type + data + crc
        if offset + chunk_total_size > length_of_data:
            raise ValueError(
                f"Truncated PNG: chunk {chunk_type!r} claims size beyond EOF"
            )

        offset += chunk_total_size

        if chunk_type == b"IEND":
            return offset

    raise ValueError("Not a valid PNG: no IEND chunk found")


def validate_ts(data: bytes, packet_size: int = TS_PACKET_SIZE) -> int:
    """
    Confirm the data looks like an MPEG-TS stream: sync byte 0x47 should
    recur every `packet_size` bytes. Returns the packet size actually
    detected (tries 188 then 204). Raises ValueError if neither works.
    """
    def sync_ok(size: int, sample_packets: int = 64) -> bool:
        if len(data) < size:
            return False
        n = min(sample_packets, len(data) // size)
        if n == 0:
            return False
        return all(data[i * size] == TS_SYNC_BYTE for i in range(n))

    for size in (packet_size, 188, 204):
        if sync_ok(size):
            return size

    raise ValueError(
        "Not a valid MPEG-TS file: no consistent 0x47 sync pattern found "
        "at 188 or 204-byte intervals"
    )


def build_polyglot(png_path: Path, ts_path: Path, output_path: Path,
                    pad_to_packet_boundary: bool = False, quiet: bool = False) -> dict:
    """
    Build the polyglot and return a metadata dict describing it:
        {
          "output_size": int,
          "png_size": int,          # size of the (trimmed) PNG portion
          "padding_size": int,      # bytes of alignment padding added, if any
          "ts_offset": int,         # byte offset in the output where TS data starts
          "ts_size": int,           # size of the TS payload
          "packet_size": int,       # detected TS packet size (188 or 204)
        }
    This lets calling code (e.g. a batch wrapper) record exactly where the
    TS payload begins in each output file without having to re-parse it.
    """
    def log(msg):
        if not quiet:
            print(msg)

    png_data = png_path.read_bytes()
    ts_data = ts_path.read_bytes()

    png_end = find_png_end(png_data)
    trimmed_png = png_data[:png_end]
    if png_end != len(png_data):
        trailing = len(png_data) - png_end
        log(f"[!] Trimmed {trailing} trailing byte(s) after PNG IEND chunk")

    packet_size = validate_ts(ts_data)
    log(f"[+] TS packet size detected: {packet_size} bytes")

    padding = b""
    if pad_to_packet_boundary:
        # Optional: pad so the TS data begins at a file offset that is a
        # multiple of the packet size. This doesn't help TS demuxers that
        # already scan for sync (they don't care about absolute offset),
        # but can help edge-case tools that assume alignment relative to
        # some fixed point. Padding is added as a private PNG chunk data
        # blob is NOT used here -- we simply pad raw bytes after IEND,
        # which PNG decoders still ignore since parsing already stopped.
        remainder = len(trimmed_png) % packet_size
        if remainder != 0:
            pad_len = packet_size - remainder
            padding = b"\x00" * pad_len
            log(f"[+] Adding {pad_len} byte(s) of padding for packet alignment")

    output_data = trimmed_png + padding + ts_data
    output_path.write_bytes(output_data)

    ts_offset = len(trimmed_png) + len(padding)

    log(f"[+] Wrote polyglot file: {output_path} ({len(output_data)} bytes)")
    log(f"    PNG portion: 0 - {len(trimmed_png)}")
    log(f"    TS portion:  {ts_offset} - {len(output_data)}")

    return {
        "output_size": len(output_data),
        "png_size": len(trimmed_png),
        "padding_size": len(padding),
        "ts_offset": ts_offset,
        "ts_size": len(ts_data),
        "packet_size": packet_size,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Create a PNG/MPEG-TS polyglot file (valid PNG image + "
                    "valid MPEG-TS stream in one file)."
    )
    parser.add_argument("png_file", type=Path, help="Path to source PNG image")
    parser.add_argument("ts_file", type=Path, help="Path to source MPEG-TS file")
    parser.add_argument("output_file", type=Path, help="Path to write the polyglot to")
    parser.add_argument(
        "--pad", action="store_true",
        help="Pad after IEND so TS data starts on a packet-size-aligned offset"
    )
    args = parser.parse_args()

    try:
        build_polyglot(args.png_file, args.ts_file, args.output_file, args.pad)
    except (ValueError, FileNotFoundError) as e:
        print(f"[x] Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
