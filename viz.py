"""
Session Sync Visualizer
------------------------
Plays back a recorded session (frames.mp4 + actions.csv, as produced by
mouse_capture_test.py) so you can eyeball whether video, keys/buttons,
and mouse deltas are actually in sync.

Layout:
  top          -> video playback
  bottom-left  -> one tile per tracked key/button, shading from gray
                  (not held) to green (held) based on that frame's
                  held-fraction
  bottom-right -> a circle with a dot showing that frame's mouse_dx/dy,
                  clamped to the circle's edge for large deltas

Controls: Play/Pause button, and a scrubber to jump to any frame.

Usage:
  python visualizer.py path/to/capture_sessions/session_0000
  (or run with no argument to get a folder picker)

Dependencies: pip install opencv-python pillow pandas
"""

import os
import sys
import tkinter as tk
from tkinter import filedialog, messagebox

import cv2
import pandas as pd
from PIL import Image, ImageTk

TRACKED_ACTION_NAMES = ["w", "a", "s", "d", "shift", "ctrl", "space", "enter", "tab", "lmb", "rmb"]

MOUSE_CIRCLE_RADIUS = 80      # canvas px
MOUSE_CIRCLE_MAX_DELTA = 100   # raw mouse_dx/dy value that maps to the circle's edge; tune if dots pin at the edge too often

ACTIVE_COLOR = (60, 200, 90)      # RGB, fully-held key/button color
INACTIVE_COLOR = (225, 225, 225)  # RGB, not-held color


def blend(c1, c2, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


def rgb_to_hex(rgb):
    return "#%02x%02x%02x" % rgb


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

        left = tk.LabelFrame(bottom, text="Keys / Buttons")
        left.pack(side="left", fill="both", expand=True, padx=4, pady=4)

        right = tk.LabelFrame(bottom, text="Mouse delta")
        right.pack(side="left", padx=4, pady=4)

        self.key_labels = {}
        # Roughly mirrors the physical WASD/space cluster: W centered above
        # S, A/D either side, space widened along the bottom, everything
        # else grouped to the right rather than in raw list order.
        positions = {
            "w":     (0, 1, 1),
            "shift": (0, 3, 1),
            "ctrl":  (0, 4, 1),
            "enter": (0, 5, 1),
            "a":     (1, 0, 1),
            "s":     (1, 1, 1),
            "d":     (1, 2, 1),
            "tab":   (1, 3, 1),
            "lmb":   (1, 4, 1),
            "rmb":   (1, 5, 1),
            "space": (2, 0, 3),
        }
        for col in range(6):
            left.grid_columnconfigure(col, weight=1)

        for name in TRACKED_ACTION_NAMES:
            row, col, colspan = positions[name]
            lbl = tk.Label(
                left, text=name.upper(), width=8, height=2,
                relief="ridge", bg=rgb_to_hex(INACTIVE_COLOR)
            )
            lbl.grid(row=row, column=col, columnspan=colspan, padx=3, pady=3, sticky="nsew")
            self.key_labels[name] = lbl

        canvas_size = MOUSE_CIRCLE_RADIUS * 2 + 20
        self.canvas = tk.Canvas(right, width=canvas_size, height=canvas_size + 20, bg="white")
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
        for name, lbl in self.key_labels.items():
            frac = float(row[name]) if name in row and pd.notna(row[name]) else 0.0
            color = blend(INACTIVE_COLOR, ACTIVE_COLOR, frac)
            lbl.config(bg=rgb_to_hex(color))

    def _update_mouse(self, row):
        dx = float(row["mouse_dx"]) if "mouse_dx" in row and pd.notna(row["mouse_dx"]) else 0.0
        dy = float(row["mouse_dy"]) if "mouse_dy" in row and pd.notna(row["mouse_dy"]) else 0.0
        cx, cy = self.circle_center
        scale = MOUSE_CIRCLE_RADIUS / MOUSE_CIRCLE_MAX_DELTA
        px = max(-MOUSE_CIRCLE_RADIUS, min(MOUSE_CIRCLE_RADIUS, dx * scale))
        py = max(-MOUSE_CIRCLE_RADIUS, min(MOUSE_CIRCLE_RADIUS, dy * scale))
        self.canvas.coords(self.dot, cx + px - 5, cy + py - 5, cx + px + 5, cy + py + 5)
        self.canvas.itemconfig(self.delta_text, text=f"dx={dx:.1f} dy={dy:.1f}")


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