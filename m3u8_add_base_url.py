#!/usr/bin/env python3
"""
Add a base URL to every relative URI in an M3U8 playlist.

This handles:
  - Segment URIs (plain lines that aren't comments/tags)
  - URI="..." attributes inside tags like EXT-X-KEY, EXT-X-MAP,
    EXT-X-MEDIA, EXT-X-I-FRAME-STREAM-INF, etc.

Absolute URLs (http://, https://, //host/path) are left untouched.

Usage:
    python m3u8_add_base_url.py input.m3u8 output.m3u8 https://example.com/videos/

If output path is omitted, the input file is overwritten.
"""

import argparse
import re
import sys
from urllib.parse import urljoin, urlparse

# Matches URI="..." or URI='...' inside a tag line, e.g.:
#   #EXT-X-KEY:METHOD=AES-128,URI="key.bin"
#   #EXT-X-MAP:URI="init.mp4"
URI_ATTR_RE = re.compile(r'URI="([^"]*)"|URI=\'([^\']*)\'')


def is_absolute(uri: str) -> bool:
    """Return True if the URI is already absolute (has a scheme or is protocol-relative)."""
    if not uri:
        return True  # nothing to do with an empty string
    if uri.startswith("//"):
        return True
    return bool(urlparse(uri).scheme)


def resolve(base_url: str, uri: str) -> str:
    """Join base_url and uri, but only if uri is relative."""
    if is_absolute(uri):
        return uri
    return urljoin(base_url, uri)


def rewrite_uri_attrs(line: str, base_url: str) -> str:
    """Rewrite any URI="..." attributes found in a tag line."""

    def _replace(match: re.Match) -> str:
        quote = '"' if match.group(1) is not None else "'"
        original = match.group(1) if match.group(1) is not None else match.group(2)
        new_uri = resolve(base_url, original)
        return f'URI={quote}{new_uri}{quote}'

    return URI_ATTR_RE.sub(_replace, line)


def process_playlist(lines, base_url: str):
    """Process all lines of an m3u8 playlist, rewriting relative URIs."""
    # Ensure base_url ends with a trailing slash so urljoin treats it as a
    # directory rather than replacing the last path segment.
    if not base_url.endswith("/"):
        base_url += "/"

    output_lines = []
    for raw_line in lines:
        line = raw_line.rstrip("\n")
        stripped = line.strip()

        if stripped.startswith("#"):
            # Tag line - may contain a URI="..." attribute (e.g. EXT-X-KEY, EXT-X-MAP)
            new_line = rewrite_uri_attrs(line, base_url)
        elif stripped == "":
            # Blank line - keep as-is
            new_line = line
        else:
            # Plain line - this is a segment or sub-playlist URI
            new_line = resolve(base_url, stripped)

        output_lines.append(new_line)

    return output_lines


def main():
    parser = argparse.ArgumentParser(
        description="Add a base URL to relative URIs in an M3U8 playlist."
    )
    parser.add_argument("input", help="Path to the input .m3u8 file")
    parser.add_argument(
        "output",
        nargs="?",
        help="Path to write the modified .m3u8 file (defaults to overwriting input)",
    )
    parser.add_argument("base_url", help="Base URL to prepend to relative URIs")
    args = parser.parse_args()

    output_path = args.output or args.input

    try:
        with open(args.input, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        print(f"Error reading {args.input}: {e}", file=sys.stderr)
        sys.exit(1)

    new_lines = process_playlist(lines, args.base_url)

    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(new_lines) + "\n")
    except OSError as e:
        print(f"Error writing {output_path}: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Wrote updated playlist to {output_path}")


if __name__ == "__main__":
    main()
