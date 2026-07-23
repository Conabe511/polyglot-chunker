#!/usr/bin/env python3
"""
tui.py - terminal UI for the polyglot chunker toolkit.

Wraps the three CLI scripts in this folder:
  - video_to_polyglot_chunks.py  (video -> folder of PNG/TS polyglot chunks)
  - png_ts_polyglot.py            (single PNG/TS polyglot builder)
  - m3u8_add_base_url.py          (rewrite relative URIs in an m3u8 playlist)

Run:
    python3 tui.py

Navigation: Up/Down to move between fields, Enter to edit a text field or
toggle a checkbox, Enter on "Run" to execute, Esc/q to go back.
"""

import curses
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable

VIDEO_SCRIPT = HERE / "video_to_polyglot_chunks.py"
POLYGLOT_SCRIPT = HERE / "png_ts_polyglot.py"
M3U8_SCRIPT = HERE / "m3u8_add_base_url.py"


class Field:
    def __init__(self, key, label, kind="text", value=""):
        self.key = key
        self.label = label
        self.kind = kind  # "text" | "bool"
        self.value = value


def edit_text(stdscr, y, x, width, initial):
    """Single-line text editor. Returns the new string, or None if cancelled (Esc)."""
    curses.curs_set(1)
    buf = list(initial)
    pos = len(buf)
    try:
        while True:
            text = "".join(buf)
            visible = text[-(width - 1):] if len(text) >= width else text
            stdscr.addstr(y, x, " " * width)
            stdscr.addstr(y, x, visible)
            stdscr.move(y, x + min(pos, width - 1))
            stdscr.refresh()
            ch = stdscr.getch()
            if ch in (curses.KEY_ENTER, 10, 13):
                return "".join(buf)
            elif ch == 27:  # Esc
                return None
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                if pos > 0:
                    del buf[pos - 1]
                    pos -= 1
            elif ch == curses.KEY_LEFT:
                pos = max(0, pos - 1)
            elif ch == curses.KEY_RIGHT:
                pos = min(len(buf), pos + 1)
            elif ch == curses.KEY_DC:
                if pos < len(buf):
                    del buf[pos]
            elif 32 <= ch < 127:
                buf.insert(pos, chr(ch))
                pos += 1
    finally:
        curses.curs_set(0)


def run_form(stdscr, title, fields):
    """
    Draw a form for `fields` (list of Field). Returns the fields list with
    updated values if the user chose Run, or None if they cancelled.
    """
    selected = 0
    items = fields + ["__RUN__"]

    while True:
        stdscr.clear()
        h, w = stdscr.getmaxyx()
        stdscr.addstr(0, 0, title, curses.A_BOLD)
        stdscr.addstr(1, 0, "-" * min(w - 1, len(title)))

        label_width = max(len(f.label) for f in fields) + 2
        row = 3
        rows = {}
        for i, item in enumerate(items):
            rows[i] = row
            if item == "__RUN__":
                attr = curses.A_REVERSE if selected == i else curses.A_BOLD
                stdscr.addstr(row + 1, 2, "[ Run ]", attr)
            else:
                attr = curses.A_REVERSE if selected == i else curses.A_NORMAL
                stdscr.addstr(row, 0, item.label.ljust(label_width))
                if item.kind == "bool":
                    box = "[x]" if item.value else "[ ]"
                    stdscr.addstr(row, label_width, box, attr)
                else:
                    val = item.value if item.value else "(empty)"
                    stdscr.addstr(row, label_width, val, attr)
                row += 1

        stdscr.addstr(h - 1, 0,
                       "Up/Down: move   Enter: edit/toggle/run   Esc/q: back",
                       curses.A_DIM)
        stdscr.refresh()

        ch = stdscr.getch()
        if ch in (curses.KEY_UP, ord("k")):
            selected = (selected - 1) % len(items)
        elif ch in (curses.KEY_DOWN, ord("j")):
            selected = (selected + 1) % len(items)
        elif ch in (27, ord("q")):
            return None
        elif ch in (curses.KEY_ENTER, 10, 13):
            item = items[selected]
            if item == "__RUN__":
                return fields
            elif item.kind == "bool":
                item.value = not item.value
            else:
                y = rows[selected]
                new_val = edit_text(stdscr, y, label_width, max(w - label_width - 1, 10),
                                     item.value)
                if new_val is not None:
                    item.value = new_val


def run_command(stdscr, cmd, cwd=None):
    """Run `cmd`, streaming its output (handles \\r progress bars) into the screen."""
    h, w = stdscr.getmaxyx()
    stdscr.clear()
    stdscr.addstr(0, 0, ("Running: " + " ".join(cmd))[: w - 1], curses.A_BOLD)
    stdscr.addstr(1, 0, "-" * (w - 1))
    out_win = curses.newwin(h - 3, w, 2, 0)
    out_win.scrollok(True)
    stdscr.addstr(h - 1, 0, "Running... (Ctrl+C to abort)", curses.A_DIM)
    stdscr.refresh()
    out_win.refresh()

    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except FileNotFoundError as exc:
        stdscr.addstr(h - 1, 0, f"Failed to start: {exc}"[: w - 1], curses.A_BOLD)
        stdscr.refresh()
        stdscr.getch()
        return

    line_buf = ""
    cur_y = 0
    max_y = h - 3

    def emit(line, overwrite=False):
        nonlocal cur_y
        if overwrite and cur_y > 0:
            out_win.move(cur_y - 1, 0)
            out_win.clrtoeol()
            out_win.addstr(cur_y - 1, 0, line[: w - 1])
        else:
            if cur_y >= max_y:
                out_win.scroll(1)
                cur_y = max_y - 1
            out_win.addstr(cur_y, 0, line[: w - 1])
            cur_y += 1
        out_win.refresh()

    try:
        while True:
            ch = proc.stdout.read(1)
            if ch == "" and proc.poll() is not None:
                break
            if ch == "\n":
                if line_buf:
                    emit(line_buf, overwrite=False)
                    line_buf = ""
            elif ch == "\r":
                if line_buf:
                    emit(line_buf, overwrite=True)
                    line_buf = ""
            elif ch:
                line_buf += ch
    except KeyboardInterrupt:
        proc.terminate()
    finally:
        if line_buf:
            emit(line_buf)
        proc.wait()

    status = "OK (exit 0)" if proc.returncode == 0 else f"FAILED (exit {proc.returncode})"
    stdscr.addstr(h - 1, 0, " " * (w - 1))
    stdscr.addstr(h - 1, 0, f"{status} -- press any key to return to menu"[: w - 1],
                  curses.A_BOLD)
    stdscr.refresh()
    stdscr.getch()


def screen_video_chunks(stdscr):
    fields = [
        Field("input", "Input video (.mp4/.mkv):"),
        Field("png", "Cover PNG:"),
        Field("output", "Output directory:"),
        Field("chunk_duration", "Chunk duration (s):", value="10"),
        Field("pad", "Pad to packet boundary", kind="bool"),
        Field("keep_raw", "Keep raw .ts chunks", kind="bool"),
        Field("hidden", "Hidden mode (randomized names)", kind="bool"),
        Field("verbose", "Verbose output", kind="bool"),
    ]
    result = run_form(stdscr, "Video -> Polyglot Chunks", fields)
    if result is None:
        return
    by_key = {f.key: f for f in result}
    if not by_key["input"].value or not by_key["png"].value or not by_key["output"].value:
        return
    cmd = [PY, str(VIDEO_SCRIPT),
           by_key["input"].value, by_key["png"].value, by_key["output"].value,
           "--chunk-duration", by_key["chunk_duration"].value or "10"]
    if by_key["pad"].value:
        cmd.append("--pad")
    if by_key["keep_raw"].value:
        cmd.append("--keep-raw")
    if by_key["hidden"].value:
        cmd.append("--hidden")
    if by_key["verbose"].value:
        cmd.append("--verbose")
    run_command(stdscr, cmd)


def screen_single_polyglot(stdscr):
    fields = [
        Field("png", "Source PNG:"),
        Field("ts", "Source TS file:"),
        Field("output", "Output file:"),
        Field("pad", "Pad to packet boundary", kind="bool"),
    ]
    result = run_form(stdscr, "Single PNG/TS Polyglot Builder", fields)
    if result is None:
        return
    by_key = {f.key: f for f in result}
    if not by_key["png"].value or not by_key["ts"].value or not by_key["output"].value:
        return
    cmd = [PY, str(POLYGLOT_SCRIPT),
           by_key["png"].value, by_key["ts"].value, by_key["output"].value]
    if by_key["pad"].value:
        cmd.append("--pad")
    run_command(stdscr, cmd)


def screen_m3u8_base_url(stdscr):
    fields = [
        Field("input", "Input .m3u8:"),
        Field("output", "Output .m3u8 (blank = overwrite input):"),
        Field("base_url", "Base URL:"),
    ]
    result = run_form(stdscr, "M3U8 Base URL Rewriter", fields)
    if result is None:
        return
    by_key = {f.key: f for f in result}
    if not by_key["input"].value or not by_key["base_url"].value:
        return
    cmd = [PY, str(M3U8_SCRIPT), by_key["input"].value]
    if by_key["output"].value:
        cmd.append(by_key["output"].value)
    cmd.append(by_key["base_url"].value)
    run_command(stdscr, cmd)


def main_menu(stdscr):
    curses.curs_set(0)
    options = [
        ("Video -> Polyglot Chunks", screen_video_chunks),
        ("Single PNG/TS Polyglot Builder", screen_single_polyglot),
        ("M3U8 Base URL Rewriter", screen_m3u8_base_url),
        ("Quit", None),
    ]
    selected = 0
    while True:
        stdscr.clear()
        h, w = stdscr.getmaxyx()
        title = "polyglot-chunker"
        stdscr.addstr(0, 0, title, curses.A_BOLD)
        stdscr.addstr(1, 0, "-" * len(title))
        for i, (label, _) in enumerate(options):
            attr = curses.A_REVERSE if i == selected else curses.A_NORMAL
            stdscr.addstr(3 + i, 2, label, attr)
        stdscr.addstr(h - 1, 0, "Up/Down: move   Enter: select   q: quit", curses.A_DIM)
        stdscr.refresh()

        ch = stdscr.getch()
        if ch in (curses.KEY_UP, ord("k")):
            selected = (selected - 1) % len(options)
        elif ch in (curses.KEY_DOWN, ord("j")):
            selected = (selected + 1) % len(options)
        elif ch == ord("q"):
            return
        elif ch in (curses.KEY_ENTER, 10, 13):
            label, func = options[selected]
            if func is None:
                return
            func(stdscr)


def main():
    curses.wrapper(main_menu)


if __name__ == "__main__":
    main()
