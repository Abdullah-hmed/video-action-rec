"""
Mouse Raw Input + Screen Capture Sync Test
--------------------------------------------
Purpose: validate that Windows Raw Input mouse deltas can be captured
independently of the OS cursor (works even when a game grabs/hides it
for mouselook), and that those deltas stay correctly synced with a
throttled screen-capture loop.

Controls:
  Insert         -> start a new recording session (only works while idle)
  Escape       -> pause / resume the current session
  Shift+Escape -> stop and finalize the current session

Output (per session, in ./capture_sessions/session_XXXX/):
  frames.mp4   -> captured frames at CONFIG["fps"], each with a debug
                  overlay (arrow + text) showing the accumulated mouse
                  delta for that frame's window -- lets you eyeball sync
                  by watching the arrow react as you move the mouse
  actions.csv  -> frame_idx, timestamp, dt, mouse_dx, mouse_dy

Suggested test order:
  1. Run standalone, no game -- move the mouse in slow deliberate patterns
     (e.g. "right then stop", "circle") and confirm the overlay arrow
     in frames.mp4 matches what you did, and dx/dy in the CSV return to
     ~0 the instant you stop moving.
  2. Run with GTA:SA focused and mouselook active, repeat the same check.
     If Escape doesn't pause both the game menu and this script's
     recording at the same time, or if the arrow stops responding while
     the game has focus, that's the thing to report back.

Dependencies: pip install mss opencv-python numpy pynput
Windows only (uses the Win32 Raw Input API via ctypes).
"""

import ctypes
from ctypes import wintypes
import threading
import time
import os
import csv

import cv2
import numpy as np
import mss
from pynput import keyboard

# ----------------------------------------------------------------------
# CONFIG -- kept variable on purpose (fps/resolution/encoder may change
# later); 5 fps / native resolution / no resize is the base case for
# this sync test.
# ----------------------------------------------------------------------
CONFIG = {
    "fps": 10,
    "output_root": "capture_sessions",
    "monitor_index": 1,       # mss monitor index (1 = primary display)
    "resize_to": (512, 512),        # e.g. (256, 256) later; None = native res for now
    "draw_debug_overlay": True,
}

# ----------------------------------------------------------------------
# Pointer-width-correct WPARAM/LPARAM.
# ctypes.wintypes.WPARAM/LPARAM are defined as plain c_ulong/c_long in
# the stdlib, which truncates on 64-bit Windows. WM_INPUT's lParam is a
# handle-sized value, so we define our own to avoid silent corruption.
# ----------------------------------------------------------------------
if ctypes.sizeof(ctypes.c_void_p) == 8:
    WPARAM = ctypes.c_uint64
    LPARAM = ctypes.c_int64
else:
    WPARAM = ctypes.c_uint32
    LPARAM = ctypes.c_int32

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WM_INPUT = 0x00FF
WM_DESTROY = 0x0002
RIDEV_INPUTSINK = 0x00000100
RID_INPUT = 0x10000003
RIM_TYPEMOUSE = 0
HWND_MESSAGE = -3
WS_OVERLAPPED = 0x00000000


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", wintypes.DWORD),
        ("dwSize", wintypes.DWORD),
        ("hDevice", wintypes.HANDLE),
        ("wParam", WPARAM),
    ]


class RAWMOUSE(ctypes.Structure):
    _fields_ = [
        ("usFlags", ctypes.c_ushort),
        ("usButtonFlags", ctypes.c_ushort),
        ("usButtonData", ctypes.c_ushort),
        ("ulRawButtons", ctypes.c_ulong),
        ("lLastX", wintypes.LONG),
        ("lLastY", wintypes.LONG),
        ("ulExtraInformation", ctypes.c_ulong),
    ]


class RAWINPUT(ctypes.Structure):
    _fields_ = [
        ("header", RAWINPUTHEADER),
        ("mouse", RAWMOUSE),
    ]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", ctypes.c_ushort),
        ("usUsage", ctypes.c_ushort),
        ("dwFlags", wintypes.DWORD),
        ("hwndTarget", wintypes.HWND),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


# Explicit argtypes/restype on the Win32 calls we use -- ctypes' default
# guessing gets 64-bit handles/pointers wrong often enough that it's
# worth pinning these down rather than debugging a garbage crash later.
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM

user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND

user32.RegisterRawInputDevices.argtypes = [
    ctypes.POINTER(RAWINPUTDEVICE), wintypes.UINT, wintypes.UINT
]
user32.RegisterRawInputDevices.restype = wintypes.BOOL

user32.GetRawInputData.argtypes = [
    ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p,
    ctypes.POINTER(wintypes.UINT), wintypes.UINT,
]
user32.GetRawInputData.restype = ctypes.c_uint

user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
user32.DefWindowProcW.restype = ctypes.c_long

user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int


class RawMouseReader:
    """
    Runs a hidden message-only window on its own thread, registers for
    raw mouse input with RIDEV_INPUTSINK (delivers events even without
    window focus -- so it still sees deltas while GTA:SA has focus and
    has grabbed the cursor), and accumulates dx/dy until drain() is
    called.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._dx = 0
        self._dy = 0
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        self._ready.wait(timeout=5)
        if not self._ready.is_set():
            raise RuntimeError("Raw input window failed to initialize within 5s")

    def drain(self):
        """Return accumulated (dx, dy) since the last call and reset to 0."""
        with self._lock:
            dx, dy = self._dx, self._dy
            self._dx = 0
            self._dy = 0
        return dx, dy

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            self._handle_raw_input(lparam)
            return 0
        elif msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _handle_raw_input(self, lparam):
        h_raw_input = ctypes.c_void_p(lparam)
        size = wintypes.UINT(0)
        user32.GetRawInputData(
            h_raw_input, RID_INPUT, None, ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER)
        )
        if size.value == 0:
            return
        buf = ctypes.create_string_buffer(size.value)
        got = user32.GetRawInputData(
            h_raw_input, RID_INPUT, buf, ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER)
        )
        if got != size.value:
            return
        raw = ctypes.cast(buf, ctypes.POINTER(RAWINPUT)).contents
        if raw.header.dwType == RIM_TYPEMOUSE:
            with self._lock:
                self._dx += raw.mouse.lLastX
                self._dy += raw.mouse.lLastY

    def _run(self):
        wndproc_ref = WNDPROC(self._wndproc)
        self._wndproc_ref = wndproc_ref  # keep alive -- ctypes won't protect this from GC otherwise

        hinstance = kernel32.GetModuleHandleW(None)
        class_name = "RawMouseReaderWindowClass"

        wc = WNDCLASSW()
        wc.style = 0
        wc.lpfnWndProc = wndproc_ref
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = hinstance
        wc.hIcon = None
        wc.hCursor = None
        wc.hbrBackground = None
        wc.lpszMenuName = None
        wc.lpszClassName = class_name

        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom:
            raise ctypes.WinError(ctypes.get_last_error())

        hwnd = user32.CreateWindowExW(
            0, class_name, "RawMouseReader", WS_OVERLAPPED,
            0, 0, 0, 0, HWND_MESSAGE, None, hinstance, None
        )
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())

        rid = RAWINPUTDEVICE()
        rid.usUsagePage = 0x01   # generic desktop controls
        rid.usUsage = 0x02       # mouse
        rid.dwFlags = RIDEV_INPUTSINK
        rid.hwndTarget = hwnd

        if not user32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(rid)):
            raise ctypes.WinError(ctypes.get_last_error())

        self._ready.set()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


# ----------------------------------------------------------------------
# KEYBOARD CONTROL (Insert / Escape / Shift+Escape)
# ----------------------------------------------------------------------
class ControlState:
    IDLE = "idle"
    RECORDING = "recording"
    PAUSED = "paused"


class SessionController:
    def __init__(self):
        self.state = ControlState.IDLE
        self._shift_down = False
        self.new_session_requested = False
        self.resume_requested = False
        self.stop_requested = False
        self._lock = threading.Lock()

    def on_press(self, key):
        if key in (keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r):
            self._shift_down = True
            return

        if key == keyboard.Key.insert:
            with self._lock:
                if self.state == ControlState.IDLE:
                    self.new_session_requested = True
                    self.state = ControlState.RECORDING
                    print("[control] INSERT -> starting new session")

        elif key == keyboard.Key.esc:
            with self._lock:
                if self._shift_down and self.state in (ControlState.RECORDING, ControlState.PAUSED):
                    self.stop_requested = True
                    self.state = ControlState.IDLE
                    print("[control] SHIFT+ESC -> stopping and finalizing session")
                elif not self._shift_down and self.state == ControlState.RECORDING:
                    self.state = ControlState.PAUSED
                    print("[control] ESC -> paused")
                elif not self._shift_down and self.state == ControlState.PAUSED:
                    self.state = ControlState.RECORDING
                    self.resume_requested = True
                    print("[control] ESC -> resumed")

    def on_release(self, key):
        if key in (keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r):
            self._shift_down = False


# ----------------------------------------------------------------------
# MAIN CAPTURE LOOP
# ----------------------------------------------------------------------
def draw_debug_overlay(frame, dx, dy):
    h, w = frame.shape[:2]
    cx, cy = w // 2, h // 2
    scale = 1  # exaggerate small deltas so the arrow is visible on screen
    end = (int(cx + dx * scale), int(cy + dy * scale))
    cv2.circle(frame, (cx, cy), 4, (0, 255, 0), -1)
    cv2.arrowedLine(frame, (cx, cy), end, (0, 0, 255), 2, tipLength=0.3)
    cv2.putText(
        frame, f"dx={dx} dy={dy}", (10, 30),
        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2
    )
    return frame


def next_session_dir(root):
    os.makedirs(root, exist_ok=True)
    existing = [d for d in os.listdir(root) if d.startswith("session_")]
    idx = len(existing)
    path = os.path.join(root, f"session_{idx:04d}")
    os.makedirs(path, exist_ok=True)
    return path


def run():
    controller = SessionController()
    listener = keyboard.Listener(on_press=controller.on_press, on_release=controller.on_release)
    listener.start()

    mouse_reader = RawMouseReader()
    mouse_reader.start()

    print("Ready. INSERT = start session, ESC = pause/resume, SHIFT+ESC = stop.")

    sct = mss.MSS()
    monitor = sct.monitors[CONFIG["monitor_index"]]

    frame_interval = 1.0 / CONFIG["fps"]

    video_writer = None
    csv_file = None
    csv_writer = None
    frame_idx = 0
    last_frame_time = None
    session_dir = None

    try:
        while True:
            if controller.new_session_requested:
                controller.new_session_requested = False
                session_dir = next_session_dir(CONFIG["output_root"])
                print(f"[session] writing to {session_dir}")

                width, height = monitor["width"], monitor["height"]
                if CONFIG["resize_to"]:
                    width, height = CONFIG["resize_to"]

                fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                video_writer = cv2.VideoWriter(
                    os.path.join(session_dir, "frames.mp4"),
                    fourcc, CONFIG["fps"], (width, height)
                )
                csv_file = open(os.path.join(session_dir, "actions.csv"), "w", newline="")
                csv_writer = csv.writer(csv_file)
                csv_writer.writerow(["frame_idx", "timestamp", "dt", "mouse_dx", "mouse_dy"])

                frame_idx = 0
                last_frame_time = time.perf_counter()
                mouse_reader.drain()  # clear any backlog accumulated before Insert was pressed

            if controller.resume_requested:
                controller.resume_requested = False
                mouse_reader.drain()  # discard whatever built up while paused
                last_frame_time = time.perf_counter()

            if controller.stop_requested:
                controller.stop_requested = False
                if video_writer is not None:
                    video_writer.release()
                    csv_file.close()
                    print(f"[session] finalized: {session_dir}")
                video_writer = None
                csv_file = None
                csv_writer = None

            if controller.state == ControlState.RECORDING and video_writer is not None:
                now = time.perf_counter()
                elapsed = now - last_frame_time
                if elapsed >= frame_interval:
                    dx, dy = mouse_reader.drain()

                    shot = sct.grab(monitor)
                    frame = np.ascontiguousarray(np.array(shot)[:, :, :3])  # BGRA -> BGR, contiguous for OpenCV
                    if CONFIG["resize_to"]:
                        frame = cv2.resize(frame, CONFIG["resize_to"])

                    if CONFIG["draw_debug_overlay"]:
                        frame = draw_debug_overlay(frame, dx, dy)

                    video_writer.write(frame)
                    csv_writer.writerow([frame_idx, now, elapsed, dx, dy])

                    frame_idx += 1
                    last_frame_time = now
                else:
                    time.sleep(max(0.0, frame_interval - elapsed))
            else:
                time.sleep(0.05)  # idle/paused: don't busy-loop

    except KeyboardInterrupt:
        pass
    finally:
        if video_writer is not None:
            video_writer.release()
        if csv_file is not None:
            csv_file.close()
        listener.stop()


if __name__ == "__main__":
    run()