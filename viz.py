"""
Session Sync Visualizer
------------------------
Plays back a recorded session (frames.mp4 + actions.csv, as produced by
cap.py) so you can eyeball whether video, keys/buttons, mouse deltas,
and scroll are actually in sync.

Layout:
  top          -> video playback
  bottom-left  -> one tile per tracked keyboard key, arranged roughly
                  like a physical keyboard (function row / number row /
                  qwerty / home / shift / bottom row, plus arrow and
                  numpad clusters), shading from gray (not held) to
                  green (held) based on that frame's held-fraction.
                  Any tracked key that doesn't fit a standard layout
                  slot (e.g. media keys, depending on your pynput
                  build) is listed in the "Other keys" row instead, so
                  nothing tracked is ever silently hidden.
  bottom-right -> lmb/rmb tiles, a circle with a dot showing that
                  frame's mouse_dx/dy (clamped to the circle's edge for
                  large deltas), and the frame's scroll delta as text.

Controls: Play/Pause button, and a scrubber to jump to any frame.

Usage:
  python viz.py path/to/capture_sessions/session_0000
  (or run with no argument to get a folder picker)

Dependencies: pip install opencv-python pillow pandas

Note: the set of tracked keys is read from actions.csv's header at
load time (every column that isn't one of the known non-key columns),
not hardcoded here -- so this stays in sync with cap.py automatically
even as its tracked-key set changes.
"""

import os
import sys
import tkinter as tk
from tkinter import filedialog, messagebox

import cv2
import pandas as pd
from PIL import Image, ImageTk

# Columns in actions.csv that aren't a tracked key/button.
NON_KEY_COLUMNS = {"frame_idx", "timestamp", "dt", "mouse_dx", "mouse_dy", "scroll"}
# Mouse buttons get their own tiles next to the mouse-delta circle,
# rather than sitting in the keyboard grid.
MOUSE_BUTTON_NAMES = {"lmb", "rmb"}

MOUSE_CIRCLE_RADIUS = 80      # canvas px
MOUSE_CIRCLE_MAX_DELTA = 100   # raw mouse_dx/dy value that maps to the circle's edge; tune if dots pin at the edge too often

ACTIVE_COLOR = (60, 200, 90)      # RGB, fully-held key/button color
INACTIVE_COLOR = (225, 225, 225)  # RGB, not-held color

# ----------------------------------------------------------------------
# Keyboard layout: (name, row, col, colspan) tuples on a shared grid.
# Each physical key has its own tracked column now (shift_l/shift_r,
# ctrl_l/ctrl_r, alt_l/alt_r), so both halves of a modifier map to
# their own tile. Only "cmd" is still a single merged column, so both
# Win-key tiles mirror the same value. alt_gr also lands in alt_r in
# cap.py, so the right-Alt tile lights up for AltGr too.
# Gaps between clusters (function-row groups, main block / arrows /
# numpad) are plain skipped columns, which is enough to read as
# keyboard-shaped without needing pixel-perfect key widths.
# ----------------------------------------------------------------------
KEYBOARD_LAYOUT = [
    # function row
    ("f1", 0, 1, 1), ("f2", 0, 2, 1), ("f3", 0, 3, 1), ("f4", 0, 4, 1),
    ("f5", 0, 6, 1), ("f6", 0, 7, 1), ("f7", 0, 8, 1), ("f8", 0, 9, 1),
    ("f9", 0, 11, 1), ("f10", 0, 12, 1), ("f11", 0, 13, 1), ("f12", 0, 14, 1),
    # number row
    ("backtick", 1, 0, 1), ("1", 1, 1, 1), ("2", 1, 2, 1), ("3", 1, 3, 1),
    ("4", 1, 4, 1), ("5", 1, 5, 1), ("6", 1, 6, 1), ("7", 1, 7, 1),
    ("8", 1, 8, 1), ("9", 1, 9, 1), ("0", 1, 10, 1), ("minus", 1, 11, 1),
    ("equals", 1, 12, 1), ("backspace", 1, 13, 2),
    # qwerty row
    ("tab", 2, 0, 1), ("q", 2, 1, 1), ("w", 2, 2, 1), ("e", 2, 3, 1),
    ("r", 2, 4, 1), ("t", 2, 5, 1), ("y", 2, 6, 1), ("u", 2, 7, 1),
    ("i", 2, 8, 1), ("o", 2, 9, 1), ("p", 2, 10, 1), ("lbracket", 2, 11, 1),
    ("rbracket", 2, 12, 1), ("backslash", 2, 13, 2),
    # home row
    ("caps_lock", 3, 0, 1), ("a", 3, 1, 1), ("s", 3, 2, 1), ("d", 3, 3, 1),
    ("f", 3, 4, 1), ("g", 3, 5, 1), ("h", 3, 6, 1), ("j", 3, 7, 1),
    ("k", 3, 8, 1), ("l", 3, 9, 1), ("semicolon", 3, 10, 1),
    ("quote", 3, 11, 1), ("enter", 3, 12, 3),
    # shift row (shift tiles on both ends, same tracked column)
    ("shift", 4, 0, 2), ("z", 4, 2, 1), ("x", 4, 3, 1), ("c", 4, 4, 1),
    ("v", 4, 5, 1), ("b", 4, 6, 1), ("n", 4, 7, 1), ("m", 4, 8, 1),
    ("comma", 4, 9, 1), ("period", 4, 10, 1), ("slash", 4, 11, 1),
    ("shift", 4, 12, 3),
    # bottom row
    ("ctrl_l", 5, 0, 1), ("cmd", 5, 1, 1), ("alt_l", 5, 2, 1),
    ("space", 5, 3, 6), ("alt_r", 5, 9, 1), ("cmd", 5, 10, 1),
    ("menu", 5, 11, 1), ("ctrl_r", 5, 12, 3),
    # arrow cluster (gap at col 15 separates it from the main block)
    ("up", 4, 16, 1),
    ("left", 5, 15, 1), ("down", 5, 16, 1), ("right", 5, 17, 1),
    # numpad cluster (gap at col 18 separates it from the arrows)
    ("num_lock", 1, 19, 1), ("numpad_divide", 1, 20, 1),
    ("numpad_multiply", 1, 21, 1), ("numpad_subtract", 1, 22, 1),
    ("numpad7", 2, 19, 1), ("numpad8", 2, 20, 1), ("numpad9", 2, 21, 1),
    ("numpad_add", 2, 22, 1),
    ("numpad4", 3, 19, 1), ("numpad5", 3, 20, 1), ("numpad6", 3, 21, 1),
    ("numpad1", 4, 19, 1), ("numpad2", 4, 20, 1), ("numpad3", 4, 21, 1),
    ("numpad0", 5, 19, 2), ("numpad_decimal", 5, 21, 1),
]
KEYBOARD_GRID_COLUMNS = 23
OTHER_KEYS_PER_ROW = 8  # wrap width for the leftover-keys section


def blend(c1, c2, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


def rgb_to_hex(rgb):
    return "#%02x%02x%02x" % rgb


def display_name(name):
    return name.replace("_", " ").upper()


class SyncVisualizer:
    def __init__(self, root, session_dir):
        self.root = root
        self.session_dir = session_dir
        video_path = os.path.join(session_dir, "frames.mp4")
        csv_path = os.path.join(session_dir, "actions.csv")

        if not os.path.exists(video_path) or not os.path.exists(csv_path):
            messagebox.showerror(
                "Missing files",
                f"Expected frames.mp4 and actions.csv inside:\n{session_dir}"
            )
            root.destroy()
            return

        self.df = pd.read_csv(csv_path)
        # Tracked keys/buttons are whatever's left in the header once the
        # known non-key columns are removed -- this is what keeps the
        # visualizer in sync with cap.py's key set without copying it.
        all_tracked = [c for c in self.df.columns if c not in NON_KEY_COLUMNS]
        self.button_names = [n for n in all_tracked if n in MOUSE_BUTTON_NAMES]
        self.key_names = [n for n in all_tracked if n not in MOUSE_BUTTON_NAMES]

        self.cap = cv2.VideoCapture(video_path)
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 10.0

        reported_count = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        # cv2's reported frame count can be off by a bit depending on container/codec;
        # trust the shorter of the two so we never index past either source.
        self.frame_count = max(1, min(reported_count, len(self.df)) if reported_count > 0 else len(self.df))

        self.playing = False
        self.current_idx = 0
        self._imgtk = None  # keep a live reference so tkinter doesn't garbage-collect the image

        self._build_ui()
        self._show_frame(0)

    # ---------------- UI construction ----------------
    def _build_ui(self):
        self.root.title(f"Sync check -- {os.path.basename(self.session_dir)}")

        top = tk.Frame(self.root)
        top.pack(side="top", fill="both", expand=True)

        self.video_label = tk.Label(top, bg="black")
        self.video_label.pack(fill="both", expand=True)

        bottom = tk.Frame(self.root)
        bottom.pack(side="top", fill="x")

        left = tk.Frame(bottom)
        left.pack(side="left", fill="both", expand=True, padx=4, pady=4)

        right = tk.LabelFrame(bottom, text="Mouse")
        right.pack(side="left", padx=4, pady=4)

        self.key_labels = {}  # name -> list of Label widgets (may be >1, e.g. shift)
        self._build_keyboard(left)

        for name in self.button_names:
            self.key_labels.setdefault(name, [])
        self._build_mouse_panel(right)

    def _build_keyboard(self, parent):
        keys_frame = tk.LabelFrame(parent, text="Keyboard")
        keys_frame.pack(side="top", fill="both", expand=True)
        for col in range(KEYBOARD_GRID_COLUMNS):
            keys_frame.grid_columnconfigure(col, weight=1)

        placed = set()
        for name, row, col, colspan in KEYBOARD_LAYOUT:
            if name not in self.key_names:
                continue
            lbl = tk.Label(
                keys_frame, text=display_name(name), width=6, height=2,
                relief="ridge", bg=rgb_to_hex(INACTIVE_COLOR), font=("TkDefaultFont", 8),
            )
            lbl.grid(row=row, column=col, columnspan=colspan, padx=2, pady=2, sticky="nsew")
            self.key_labels.setdefault(name, []).append(lbl)
            placed.add(name)

        leftover = sorted(n for n in self.key_names if n not in placed)
        if leftover:
            other_frame = tk.LabelFrame(parent, text="Other keys")
            other_frame.pack(side="top", fill="x", pady=(4, 0))
            for col in range(OTHER_KEYS_PER_ROW):
                other_frame.grid_columnconfigure(col, weight=1)
            for i, name in enumerate(leftover):
                r, c = divmod(i, OTHER_KEYS_PER_ROW)
                lbl = tk.Label(
                    other_frame, text=display_name(name), width=8, height=2,
                    relief="ridge", bg=rgb_to_hex(INACTIVE_COLOR), font=("TkDefaultFont", 8),
                )
                lbl.grid(row=r, column=c, padx=2, pady=2, sticky="nsew")
                self.key_labels.setdefault(name, []).append(lbl)

    def _build_mouse_panel(self, parent):
        buttons_row = tk.Frame(parent)
        buttons_row.pack(side="top", pady=(4, 0))
        for name in self.button_names:
            lbl = tk.Label(
                buttons_row, text=display_name(name), width=8, height=2,
                relief="ridge", bg=rgb_to_hex(INACTIVE_COLOR),
            )
            lbl.pack(side="left", padx=3)
            self.key_labels[name].append(lbl)

        canvas_size = MOUSE_CIRCLE_RADIUS * 2 + 20
        self.canvas = tk.Canvas(parent, width=canvas_size, height=canvas_size + 20, bg="white")
        self.canvas.pack(padx=4, pady=4)

        cx = cy = MOUSE_CIRCLE_RADIUS + 10
        self.circle_center = (cx, cy)
        self.canvas.create_oval(
            cx - MOUSE_CIRCLE_RADIUS, cy - MOUSE_CIRCLE_RADIUS,
            cx + MOUSE_CIRCLE_RADIUS, cy + MOUSE_CIRCLE_RADIUS,
            outline="black", width=2,
        )
        self.dot = self.canvas.create_oval(cx - 5, cy - 5, cx + 5, cy + 5, fill="red")
        self.delta_text = self.canvas.create_text(cx, canvas_size, text="dx=0.0 dy=0.0")

        # Scroll delta, shown in the corner of the mouse panel.
        self.scroll_label = tk.Label(parent, text="scroll: 0", anchor="e")
        self.scroll_label.pack(side="top", fill="x", padx=4, pady=(0, 4))

        controls = tk.Frame(self.root)
        controls.pack(side="top", fill="x")

        self.play_btn = tk.Button(controls, text="Play", width=8, command=self.toggle_play)
        self.play_btn.pack(side="left", padx=4, pady=4)

        self.frame_label = tk.Label(controls, text=f"0 / {self.frame_count - 1}")
        self.frame_label.pack(side="left", padx=8)

        self.scale = tk.Scale(
            controls, from_=0, to=self.frame_count - 1, orient="horizontal",
            showvalue=False, command=self._on_scale
        )
        self.scale.pack(side="left", fill="x", expand=True, padx=4)

    # ---------------- playback ----------------
    def toggle_play(self):
        self.playing = not self.playing
        self.play_btn.config(text="Pause" if self.playing else "Play")
        if self.playing:
            self._tick()

    def _tick(self):
        if not self.playing:
            return
        next_idx = self.current_idx + 1
        if next_idx >= self.frame_count:
            self.playing = False
            self.play_btn.config(text="Play")
            return
        self._show_frame(next_idx)
        self.root.after(max(1, int(1000 / self.fps)), self._tick)

    def _on_scale(self, value):
        idx = int(float(value))
        if idx != self.current_idx:
            self._show_frame(idx)

    # ---------------- rendering ----------------
    def _show_frame(self, idx):
        idx = max(0, min(idx, self.frame_count - 1))
        self.current_idx = idx

        self.cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ret, frame = self.cap.read()
        if ret:
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(frame_rgb)
            img.thumbnail((720, 480))  # fit the display area, keep aspect ratio
            self._imgtk = ImageTk.PhotoImage(img)
            self.video_label.configure(image=self._imgtk)

        row = self.df.iloc[idx]
        self._update_keys(row)
        self._update_mouse(row)

        self.frame_label.config(text=f"{idx} / {self.frame_count - 1}")
        self.scale.set(idx)

    def _update_keys(self, row):
        for name, widgets in self.key_labels.items():
            frac = float(row[name]) if name in row and pd.notna(row[name]) else 0.0
            color = rgb_to_hex(blend(INACTIVE_COLOR, ACTIVE_COLOR, frac))
            for lbl in widgets:
                lbl.config(bg=color)

    def _update_mouse(self, row):
        dx = float(row["mouse_dx"]) if "mouse_dx" in row and pd.notna(row["mouse_dx"]) else 0.0
        dy = float(row["mouse_dy"]) if "mouse_dy" in row and pd.notna(row["mouse_dy"]) else 0.0
        cx, cy = self.circle_center
        scale = MOUSE_CIRCLE_RADIUS / MOUSE_CIRCLE_MAX_DELTA
        px = max(-MOUSE_CIRCLE_RADIUS, min(MOUSE_CIRCLE_RADIUS, dx * scale))
        py = max(-MOUSE_CIRCLE_RADIUS, min(MOUSE_CIRCLE_RADIUS, dy * scale))
        self.canvas.coords(self.dot, cx + px - 5, cy + py - 5, cx + px + 5, cy + py + 5)
        self.canvas.itemconfig(self.delta_text, text=f"dx={dx:.1f} dy={dy:.1f}")

        scroll = row["scroll"] if "scroll" in row and pd.notna(row["scroll"]) else 0
        self.scroll_label.config(text=f"scroll: {scroll:g}")


def main():
    root = tk.Tk()
    if len(sys.argv) > 1:
        session_dir = sys.argv[1]
    else:
        session_dir = filedialog.askdirectory(
            title="Select a session folder (containing frames.mp4 + actions.csv)"
        )
        if not session_dir:
            return
    SyncVisualizer(root, session_dir)
    root.mainloop()


if __name__ == "__main__":
    main()