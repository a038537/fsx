#!/usr/bin/env python3
# fsx_menu.py — Fullscreen menu for FSX, with PiP mpv preview, looping bg audio, and an inline TV Guide pane
# Endpoints used by fsx_zap.lua router:
#   GET  /menu/visible
#   POST /menu/toggle, /menu/open, /menu/close
#   POST /menu/nav/up, /menu/nav/down, /menu/enter, /menu/esc
#   POST /menu/nav/left, /menu/nav/right, /menu/nav/pageup, /menu/nav/pagedown   (guide mode)
#   POST /menu/guide                         (optional: jump straight to guide)
#   POST /menu/select/<idx|label>            (select a menu item programmatically)
#   POST /menu/activate/<idx|label>          (select+enter in one call)

from __future__ import annotations
import os, sys, json, socket, threading, argparse, subprocess, sqlite3, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional, Tuple, List, Dict
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF
from PySide6.QtGui  import QGuiApplication, QPainter, QColor, QFont, QPixmap, QFontMetrics, QPen
from PySide6.QtWidgets import QApplication, QWidget

# -------------------- Paths & Defaults --------------------
ROOT = Path(os.environ.get("FSX_ROOT", ".")).resolve()
DEFAULT_LOGO = ROOT / "static" / "logos" / "sky.png"
SCHED_DB = ROOT / "schedules" / "fsx_schedule.sqlite"

# Default menu background audio
DEFAULT_MENU_AUDIO = Path(os.environ.get("FSX_MENU_AUDIO", str(ROOT / "static" / "audio" / "sky_bassophere.mp3")))
MENU_AUDIO_VOL = int(os.environ.get("FSX_MENU_AUDIO_VOL", "45"))  # 0..100

# Base colors
BG_COLOR   = QColor("#0a1f3f")
TEXT_MAIN  = QColor("#e9f2ff")
TEXT_MUTED = QColor("#b8c6e6")
HILITE     = QColor("#68a8c3")   # divider + accents
GRID_ACC   = QColor("#2b476f")   # guide grid lines (subtle)

MENU_ITEMS = ["Live TV", "TV Guide", "Planner", "OnDemand"]

# Player integration
PLAYER_HOST = os.environ.get("FSX_HOST", "127.0.0.1")
PLAYER_PORT = int(os.environ.get("FSX_PORT", "4243"))
MPV_SOCK    = os.environ.get("FSX_MPV_SOCKET", "/tmp/fsx_mpv.sock")
TZ_NAME     = os.environ.get("FSX_TZ", "Europe/Brussels")

# Prefer X11 on Pi / Xwayland
os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
GPU_CONTEXT = os.environ.get("FSX_GPU_CONTEXT", "x11egl")
HWDEC       = os.environ.get("FSX_HWDEC", "auto-safe")
ALANG       = os.environ.get("FSX_ALANG", "eng,en")
SLANG       = os.environ.get("FSX_SLANG", "dut,nld")

# Header fonts (relative to screen height)
DATE_PT_FRAC = 0.014  # date line
TIME_PT_FRAC = 0.018  # time line (bold)
TIME_NUDGE_UP_FRAC = float(os.environ.get("FSX_MENU_TIME_NUDGE_FRAC", "0.015"))

# Button layout (text-only)
BTN_COL_W_FRAC  = 0.24
BTN_GAP_Y_FRAC  = 0.016
BTN_TOP_PAD_FR  = 0.08
BTN_PT_FRAC     = 0.016
ACTIVE_SCALE    = float(os.environ.get("FSX_MENU_ACTIVE_SCALE", "1.16"))

# PiP preview box (fixed 16:9 inside this rect)
PREV_W_FRAC = 0.52
PREV_H_FRAC = 0.30
PREV_MARGIN = 0.04

# -------- PiP footer bar --------
FOOTER_H_FRAC = 0.16
FOOTER_BG     = QColor("#ffffff")
FOOTER_FG     = QColor("#0a1f3f")

# -------- Guide layout knobs --------
GUIDE_LEFT_PAD_FR   = 0.02     # left/right padding (fraction of width)
GUIDE_RIGHT_PAD_FR  = 0.02
GUIDE_ROW_GAP_FR    = 0.005
GUIDE_ROW_PT_FR     = 0.016    # base font size for events
GUIDE_MAX_ROWS      = 8
GUIDE_CELL_MIN_W_FR = 0.12     # minimum relative width per event cell (simple, fixed-width layout)

# -------------------- Simple HTTP helpers --------------------
def http_post(url: str, timeout: float = 0.5) -> tuple[bool,int,str]:
    import urllib.request, urllib.error
    try:
        req = urllib.request.Request(url, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, r.getcode(), (r.read().decode("utf-8") or "")
    except urllib.error.HTTPError as e:
        return False, e.code, e.read().decode("utf-8", "ignore")
    except Exception as e:
        return False, 0, str(e)

def http_get_json(url: str, timeout: float = 0.6) -> Optional[dict]:
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except Exception:
        return None

# -------------------- mpv IPC helpers (main player fallback) --------------------
def _mpv_ipc(cmd: list) -> bool:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.4)
        s.connect(MPV_SOCK)
        s.sendall((json.dumps({"command": cmd}) + "\n").encode())
        try: s.recv(4096)
        except Exception: pass
        s.close()
        return True
    except Exception:
        return False

def mpv_pause_on():
    _mpv_ipc(["set_property", "pause", True])
    _mpv_ipc(["set_property", "mute", True])

def mpv_pause_off():
    _mpv_ipc(["set_property", "pause", False])
    _mpv_ipc(["set_property", "mute", False])

# -------------------- Schedule helpers --------------------
def _safe_title_from_path(path: str) -> str:
    base = Path(path).name
    stem = Path(base).stem
    return stem.replace("_"," ").replace("."," ").strip() or base

def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        cols = {r[1] for r in rows}
        return column in cols
    except Exception:
        return False

def resolve_now_path_and_offset(ch_id: str) -> Optional[Tuple[str, float]]:
    """Return (path, offset_sec) for current event of channel."""
    try:
        if not SCHED_DB.exists():
            return None
        now = int(time.time())
        conn = sqlite3.connect(f"file:{SCHED_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT path, start_utc FROM schedule_events "
            "WHERE channel_id=? AND start_utc<=? AND end_utc>? "
            "ORDER BY start_utc DESC LIMIT 1",
            (str(ch_id), now, now)
        ).fetchone()
        conn.close()
        if not row:
            return None
        path = row["path"]
        start_utc = int(row["start_utc"])
        if not path:
            return None
        offset = max(0, now - start_utc)
        return (path, float(offset))
    except Exception:
        return None

def load_channels_and_events(now_ts: int) -> List[Dict]:
    """
    Returns a list of channels:
      { "id": "1", "name": "Channel 1", "events": [ {title,start,end}, ... ] }
    Events include the current one and the next few upcoming.
    Works with minimal schemas (no channel_name/title columns).
    """
    out: List[Dict] = []
    if not SCHED_DB.exists():
        return out
    conn = sqlite3.connect(f"file:{SCHED_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        has_channel_name = _table_has_column(conn, "schedule_events", "channel_name")
        has_title        = _table_has_column(conn, "schedule_events", "title")
        has_path         = _table_has_column(conn, "schedule_events", "path")

        # channel list
        chans = conn.execute(
            "SELECT DISTINCT channel_id FROM schedule_events ORDER BY CAST(channel_id AS INTEGER) ASC"
        ).fetchall()

        for ch in chans:
            ch_id = str(ch["channel_id"])

            # Channel name if exists, else fallback
            if has_channel_name:
                name_row = conn.execute(
                    "SELECT channel_name FROM schedule_events "
                    "WHERE channel_id=? AND channel_name IS NOT NULL AND channel_name!='' "
                    "LIMIT 1",
                    (ch_id,)
                ).fetchone()
                ch_name = (name_row["channel_name"].strip() if name_row and name_row["channel_name"] else f"Channel {ch_id}")
            else:
                ch_name = f"Channel {ch_id}"

            # current + next 5 events
            ev_rows = conn.execute(
                "SELECT " +
                ( "title," if has_title else "" ) +
                ( "path,"  if has_path  else "" ) +
                " start_utc, end_utc "
                "FROM schedule_events "
                "WHERE channel_id=? AND end_utc>? "
                "ORDER BY start_utc ASC LIMIT 6",
                (ch_id, now_ts)
            ).fetchall()

            events = []
            for r in ev_rows:
                start_ = int(r["start_utc"])
                end_   = int(r["end_utc"])
                if end_ <= start_:
                    end_ = start_ + 60

                title = ""
                if has_title and "title" in r.keys():
                    title = (r["title"] or "").strip()
                if not title and has_path and "path" in r.keys():
                    title = _safe_title_from_path(r["path"] or "")
                if not title:
                    title = "—"

                events.append({"title": title, "start": start_, "end": end_})

            if not events:
                events = [{"title": "—", "start": now_ts, "end": now_ts + 3600}]

            out.append({"id": ch_id, "name": ch_name, "events": events})
    finally:
        conn.close()
    return out

# -------------------- PiP Footer Widget --------------------
class PiPFooter(QWidget):
    """Small white footer over the PiP showing channel number and name."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.ch_num  = ""
        self.ch_name = ""
        self.hide()

    def set_info(self, ch_num: str, ch_name: str):
        self.ch_num = ch_num or ""
        self.ch_name = ch_name or ""
        if self.ch_num or self.ch_name:
            self.show()
        else:
            self.hide()
        self.update()

    def paintEvent(self, ev):
        if not (self.ch_num or self.ch_name): return
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, FOOTER_BG)
        left_pad  = max(6, int(h * 0.12))
        right_pad = max(6, int(h * 0.08))
        gap_x     = max(6, int(h * 0.10))
        f_num = QFont("Sans");  f_num.setBold(True);  f_num.setPixelSize(max(10, int(h * 0.55)))
        f_name = QFont("Sans"); f_name.setPixelSize(max(10, int(h * 0.45)))
        p.setPen(FOOTER_FG)
        p.setFont(f_num); fm_num = QFontMetrics(f_num)
        base_y_num = (h + fm_num.ascent() - fm_num.descent()) // 2
        x = left_pad
        if self.ch_num:
            p.drawText(x, base_y_num, str(self.ch_num))
            x += fm_num.horizontalAdvance(str(self.ch_num)) + gap_x
        p.setFont(f_name); fm_name = QFontMetrics(f_name)
        avail = max(0, w - right_pad - x)
        name_txt = fm_name.elidedText(self.ch_name or "", Qt.ElideRight, avail)
        base_y_name = (h + fm_name.ascent() - fm_name.descent()) // 2
        p.drawText(x, base_y_name, name_txt)
        p.end()

# -------------------- Preview widget (embeds mpv with --wid) --------------------
class PreviewWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.proc: Optional[subprocess.Popen] = None

    def start_preview(self, path: str, start_sec: float):
        self.stop_preview()
        try:
            wid = int(self.winId())
            args = [
                "mpv", path,
                "--no-config", "--no-input-default-bindings",
                "--mute=yes", "--no-osc", "--osd-level=0",
                "--hr-seek=no", "--profile=low-latency",
                "--keep-open=yes",
                f"--start={max(0.0, float(start_sec)):.3f}",
                f"--wid={wid}", "--no-border", "--force-window=yes",
                "--vo=gpu", f"--gpu-context={GPU_CONTEXT}",
                f"--hwdec={HWDEC}",
                f"--alang={ALANG}", f"--slang={SLANG}",
                "--video-aspect-override=16:9", "--keepaspect=yes",
            ]
            self.proc = subprocess.Popen(args)
        except Exception:
            self.proc = None

    def stop_preview(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                try: self.proc.wait(timeout=0.8)
                except subprocess.TimeoutExpired: self.proc.kill()
            except Exception: pass
        self.proc = None

    def closeEvent(self, ev):
        self.stop_preview()
        super().closeEvent(ev)

# -------------------- Menu background audio (looped) --------------------
class MenuAudio:
    def __init__(self, path: Path, volume: int = 45):
        self.path = Path(path)
        self.volume = max(0, min(100, int(volume)))
        self.proc: Optional[subprocess.Popen] = None

    def start(self):
        if not self.path.exists(): return
        self.stop()
        try:
            args = [
                "mpv", str(self.path),
                "--no-config", "--no-video", "--loop-file=inf",
                "--really-quiet", "--no-osc", "--osd-level=0",
                f"--volume={self.volume}",
                "--audio-channels=stereo",
                "--audio-buffer=0.3",   # slightly larger buffer to avoid underrun
            ]
            self.proc = subprocess.Popen(args)
        except Exception:
            self.proc = None

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                try: self.proc.wait(timeout=0.8)
                except subprocess.TimeoutExpired: self.proc.kill()
            except Exception: pass
        self.proc = None

# -------------------- HTTP control for the menu --------------------
class _Http(BaseHTTPRequestHandler):
    window: "MenuWindow" = None
    def log_message(self, fmt, *args): print(f"[MENU HTTP] {fmt % args}", flush=True)

    def _ok(self, payload):
        b = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def _bad(self, msg, code=400):
        b = json.dumps({"ok": False, "error": msg}).encode()
        self.send_response(code)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        if self.path == "/menu/visible":
            return self._ok({"ok": True, "visible": self.window.isVisible()})
        return self._bad("unknown", 404)

    def do_POST(self):
        path = self.path
        w = self.window

        # open/close/toggle
        if path == "/menu/open":
            w.open_menu();  return self._ok({"ok": True, "action": "open"})
        if path == "/menu/close":
            w.close_menu(); return self._ok({"ok": True, "action": "close"})
        if path == "/menu/toggle":
            vis = not w.isVisible()
            (w.open_menu() if vis else w.close_menu())
            return self._ok({"ok": True, "visible": vis})

        # If not visible, ignore nav
        if not w.isVisible():
            return self._ok({"ok": True, "ignored": "not-visible"})

        # Root-mode navigation
        if path == "/menu/nav/up":     w.nav_up();    return self._ok({"ok": True})
        if path == "/menu/nav/down":   w.nav_down();  return self._ok({"ok": True})

        # Accept common Enter aliases
        if path in ("/menu/enter", "/menu/nav/enter", "/menu/key/enter", "/menu/ok", "/menu/select"):
            w.nav_enter(); return self._ok({"ok": True})

        if path == "/menu/esc":        w.close_menu();return self._ok({"ok": True})

        # Guide-mode navigation (fallback to root semantics when not in guide)
        if path == "/menu/nav/left":
            if getattr(w, "mode", "root") == "guide":
                w.guide_left()
            else:
                w.nav_up()
            return self._ok({"ok": True})

        if path == "/menu/nav/right":
            if getattr(w, "mode", "root") == "guide":
                w.guide_right()
            else:
                w.nav_down()
            return self._ok({"ok": True})

        if path == "/menu/nav/pageup":
            if getattr(w, "mode", "root") == "guide":
                w.guide_page_up()
            else:
                w.nav_up()
            return self._ok({"ok": True})

        if path == "/menu/nav/pagedown":
            if getattr(w, "mode", "root") == "guide":
                w.guide_page_down()
            else:
                w.nav_down()
            return self._ok({"ok": True})

        # Optional quick intent: go straight to guide
        if path == "/menu/guide":
            w._enter_guide_mode()
            return self._ok({"ok": True, "action": "guide"})

        # Programmatic selection/activation
        if path.startswith("/menu/select/"):
            key = path[len("/menu/select/"):]
            sel_ok = False
            try:
                sel_ok = w._select_item(int(key))
            except ValueError:
                sel_ok = w._select_item(key)
            return self._ok({"ok": sel_ok, "selected": (w.items[w.sel] if sel_ok else None)})

        if path.startswith("/menu/activate/"):
            key = path[len("/menu/activate/"):]
            if key:  # if a key is provided, select it first
                try:
                    if not w._select_item(int(key)):
                        return self._ok({"ok": False, "error": "no such item"})
                except ValueError:
                    if not w._select_item(key):
                        return self._ok({"ok": False, "error": "no such item"})
            w._activate_selection()
            return self._ok({"ok": True, "activated": True})

        return self._bad("unknown", 404)

def start_http(window: "MenuWindow", host="127.0.0.1", port=9402):
    _Http.window = window
    srv = HTTPServer((host, port), _Http)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    print(f"[MENU] HTTP at http://{host}:{port}", flush=True)
    return srv

# -------------------- Menu Window --------------------
class MenuWindow(QWidget):
    def __init__(self, logo_path: Optional[Path] = None, audio_path: Optional[Path] = None, audio_vol: int = MENU_AUDIO_VOL):
        flags = (Qt.FramelessWindowHint |
                 Qt.WindowStaysOnTopHint |
                 Qt.X11BypassWindowManagerHint)
        super().__init__(None, flags)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.StrongFocus)

        scr = QGuiApplication.primaryScreen()
        self.setGeometry(scr.geometry())

        self.items = MENU_ITEMS[:]
        self.sel = 0
        self.sel_last = 0   # remember last selected item across opens

        self.logo_path = Path(logo_path) if logo_path else DEFAULT_LOGO
        self.logo: Optional[QPixmap] = None
        self._load_logo()

        # PiP preview child
        self.preview = PreviewWidget(self)
        self._layout_preview()

        # PiP footer overlay
        self.pip_footer = PiPFooter(self)
        self.pip_footer.hide()

        # Menu audio
        self.menu_audio = MenuAudio(path=(audio_path or DEFAULT_MENU_AUDIO), volume=audio_vol)

        # repaint at 1/2s for clock updates
        self.repaint_timer = QTimer(self)
        self.repaint_timer.setInterval(500)
        self.repaint_timer.timeout.connect(self.update)
        self.repaint_timer.start()

        self.tz = ZoneInfo(TZ_NAME) if ZoneInfo else None

        # ---- Guide state ----
        self.mode: str = "root"  # "root" | "guide"
        self.guide_rows: List[Dict] = []    # loaded from DB
        self.g_sel_row: int = 0             # absolute row index in guide_rows
        self.g_sel_col: int = 0             # event column per row
        self.g_row_offset: int = 0          # top visible row index
        self.g_max_visible: int = GUIDE_MAX_ROWS

    # ---- geometry for preview (flush to top-right) ----
    def _layout_preview(self):
        W = max(1, self.width())
        H = max(1, self.height())

        max_w = max(1, int(W * PREV_W_FRAC))
        max_h = max(1, int(H * PREV_H_FRAC))
        target_ar = 16 / 9
        if max_w / max_h > target_ar:
            ph = max_h
            pw = max(1, int(ph * target_ar))
        else:
            pw = max_w
            ph = max(1, int(pw / target_ar))

        px = W - pw
        py = 0

        if getattr(self, "preview", None) is not None:
            self.preview.setGeometry(px, py, pw, ph)
            self.preview.show()
            self.preview.raise_()

        if getattr(self, "pip_footer", None) is not None:
            fh = max(1, int(ph * FOOTER_H_FRAC))
            self.pip_footer.setGeometry(px, py + ph - fh, pw, fh)
            self.pip_footer.raise_()

    def resizeEvent(self, ev):
        self._layout_preview()
        return super().resizeEvent(ev)

    def _load_logo(self):
        if self.logo_path.exists():
            pm = QPixmap(str(self.logo_path))
            self.logo = pm if not pm.isNull() else None
        else:
            self.logo = None

    # ---- Playback control ----
    def _pause_playback(self):
        ok, code, _ = http_post(f"http://{PLAYER_HOST}:{PLAYER_PORT}/pause")
        if ok and code == 200: return
        mpv_pause_on()

    def _return_to_live(self):
        ok, code, _ = http_post(f"http://{PLAYER_HOST}:{PLAYER_PORT}/live")
        if ok and code == 200: return
        st = http_get_json(f"http://{PLAYER_HOST}:{PLAYER_PORT}/status")
        ch = str(st.get("channel")) if isinstance(st, dict) and st.get("channel") is not None else None
        if ch:
            ok2, code2, _ = http_post(f"http://{PLAYER_HOST}:{PLAYER_PORT}/zap/{ch}")
            if ok2 and code2 == 200:
                mpv_pause_off(); return
        mpv_pause_off()

    # ---- Visibility ----
    def open_menu(self):
        self.showFullScreen()
        self.raise_()
        self.activateWindow()
        self.setFocus()
        self._pause_playback()
        self._start_preview_from_status()
        self._update_pip_footer_from_status()
        self.preview.raise_()
        self.pip_footer.raise_()
        self.menu_audio.start()

        # Reset to root mode on open and restore last selection
        self.mode = "root"
        self.sel = getattr(self, "sel_last", 0)
        self.update()

    def close_menu(self):
        self.menu_audio.stop()
        self.preview.stop_preview()
        self.pip_footer.hide()
        self.hide()
        self._return_to_live()

    def _start_preview_from_status(self):
        st = http_get_json(f"http://{PLAYER_HOST}:{PLAYER_PORT}/status")
        ch = None
        if isinstance(st, dict):
            ch = st.get("channel")
        if ch is None:
            return
        pair = resolve_now_path_and_offset(str(ch))
        if not pair:
            return
        path, off = pair
        if not Path(path).exists():
            return
        self.preview.start_preview(path, off)

    def _start_preview_from_channel(self, ch_id: str):
        pair = resolve_now_path_and_offset(str(ch_id))
        if not pair: return
        path, off = pair
        if not Path(path).exists(): return
        self.preview.start_preview(path, off)

    # ---- Update PiP footer from /status ----
    def _update_pip_footer_from_status(self):
        st = http_get_json(f"http://{PLAYER_HOST}:{PLAYER_PORT}/status") or {}
        ch_num  = st.get("channel") or st.get("channel_id") or st.get("ch") or ""
        ch_name = st.get("channel_name") or st.get("name") or st.get("ch_name") or ""
        ch_num_s = str(ch_num) if ch_num is not None and ch_num != "" else ""
        if not ch_name and ch_num_s:
            ch_name = f"Channel {ch_num_s}"
        try:
            self.pip_footer.set_info(ch_num_s, ch_name)
            self.pip_footer.show()
            self.pip_footer.raise_()
        except Exception:
            pass

    # ---------------- Root-mode navigation ----------------
    def nav_up(self):
        if self.mode != "root": return
        self.sel = (self.sel - 1) % len(self.items)
        self.sel_last = self.sel
        self.update()

    def nav_down(self):
        if self.mode != "root": return
        self.sel = (self.sel + 1) % len(self.items)
        self.sel_last = self.sel
        self.update()

    def nav_enter(self):
        if self.mode == "root":
            self.sel_last = self.sel
            label = self.items[self.sel]
            if label == "Live TV":
                self.close_menu()
                return
            if label == "TV Guide":
                self._enter_guide_mode()
                return
            if label == "OnDemand":
                # Example: if channel 0 opens OnDemand UI, uncomment:
                # http_post(f"http://{PLAYER_HOST}:{PLAYER_PORT}/zap/0")
                self.close_menu()
                return
            if label == "Planner":
                # Placeholder — later this can open a planner page/pane
                self.close_menu()
                return
        elif self.mode == "guide":
            # Zap to selected channel & close
            ch_id = self._guide_current_channel_id()
            if ch_id:
                http_post(f"http://{PLAYER_HOST}:{PLAYER_PORT}/zap/{ch_id}")
            self.close_menu()

    # ---- programmatic selection/activation helpers ----
    def _select_item(self, target: str|int) -> bool:
        """Select by index or label; returns True if successful."""
        if isinstance(target, int):
            if 0 <= target < len(self.items):
                self.sel = target
                self.sel_last = self.sel
                self.update()
                return True
            return False
        try:
            idx = self.items.index(str(target))
            self.sel = idx
            self.sel_last = self.sel
            self.update()
            return True
        except ValueError:
            return False

    def _activate_selection(self):
        """Behave like pressing Enter on current selection."""
        self.nav_enter()

    # ---------------- Guide-mode helpers & nav ----------------
    def _enter_guide_mode(self):
        try:
            now_ts = int(time.time())
            self.guide_rows = load_channels_and_events(now_ts)
        except Exception as e:
            print(f"[MENU] guide load failed: {e}", flush=True)
            self.guide_rows = []

        self.g_sel_row = 0
        self.g_sel_col = 0
        self.g_row_offset = 0
        self.mode = "guide"

        # Tune PiP to first channel
        ch_id = self._guide_current_channel_id()
        if ch_id:
            self._start_preview_from_channel(ch_id)
            ch_name = self.guide_rows[self.g_sel_row]["name"] if self.guide_rows else ""
            self.pip_footer.set_info(str(ch_id), ch_name)
            self.pip_footer.show(); self.pip_footer.raise_()
        self.update()

    def _guide_current_channel_id(self) -> Optional[str]:
        if not self.guide_rows: return None
        row = max(0, min(self.g_sel_row, len(self.guide_rows)-1))
        return self.guide_rows[row]["id"]

    def guide_left(self):
        if self.mode != "guide": return
        self.g_sel_col = max(0, self.g_sel_col - 1)
        self.update()

    def guide_right(self):
        if self.mode != "guide": return
        if not self.guide_rows: return
        row = self.guide_rows[self.g_sel_row]
        max_col = max(0, len(row["events"]) - 1)
        self.g_sel_col = min(max_col, self.g_sel_col + 1)
        self.update()

    def guide_page_up(self):
        if self.mode != "guide": return
        if not self.guide_rows: return
        self.g_sel_row = max(0, self.g_sel_row - 1)
        if self.g_sel_row < self.g_row_offset:
            self.g_row_offset = self.g_sel_row
        ch_id = self._guide_current_channel_id()
        if ch_id:
            self._start_preview_from_channel(ch_id)
            ch_name = self.guide_rows[self.g_sel_row]["name"]
            self.pip_footer.set_info(str(ch_id), ch_name)
        self.update()

    def guide_page_down(self):
        if self.mode != "guide": return
        if not self.guide_rows: return
        self.g_sel_row = min(len(self.guide_rows)-1, self.g_sel_row + 1)
        if self.g_sel_row >= self.g_row_offset + self.g_max_visible:
            self.g_row_offset = self.g_sel_row - self.g_max_visible + 1
        ch_id = self._guide_current_channel_id()
        if ch_id:
            self._start_preview_from_channel(ch_id)
            ch_name = self.guide_rows[self.g_sel_row]["name"]
            self.pip_footer.set_info(str(ch_id), ch_name)
        self.update()

    # ---- Keys (backup; primary input comes from lua router) ----
    def keyPressEvent(self, e):
        k = e.key()
        if k == Qt.Key_Escape:
            self.close_menu(); return
        if k == Qt.Key_Home:
            if self.isVisible(): self.close_menu()
            else: self.open_menu()
            return
        if not self.isVisible():
            e.ignore(); return

        if self.mode == "root":
            if k in (Qt.Key_Up, Qt.Key_Left):
                self.nav_up(); return
            if k in (Qt.Key_Down, Qt.Key_Right):
                self.nav_down(); return
            if k in (Qt.Key_Return, Qt.Key_Enter):
                self.nav_enter(); return
            if k == Qt.Key_PageUp:
                self.sel = (self.sel - 2) % len(self.items); self.sel_last = self.sel; self.update(); return
            if k == Qt.Key_PageDown:
                self.sel = (self.sel + 2) % len(self.items); self.sel_last = self.sel; self.update(); return

        elif self.mode == "guide":
            if k == Qt.Key_Left:
                self.guide_left(); return
            if k == Qt.Key_Right:
                self.guide_right(); return
            if k in (Qt.Key_PageUp, Qt.Key_Up):
                self.guide_page_up(); return
            if k in (Qt.Key_PageDown, Qt.Key_Down):
                self.guide_page_down(); return
            if k in (Qt.Key_Return, Qt.Key_Enter):
                self.nav_enter(); return

        e.ignore()

    # ---- Helpers ----
    def _now_tz(self) -> datetime:
        if self.tz:
            return datetime.now(self.tz)
        return datetime.now()

    def _date_text(self, dt: datetime) -> str:
        return dt.strftime("%A, %d %B")

    def _time_text(self, dt: datetime) -> str:
        return dt.strftime("%H:%M")

    # ---- Painting ----
    def paintEvent(self, ev):
        p = QPainter(self)
        try:
            W, H = self.width(), self.height()
            p.fillRect(QRectF(0, 0, W, H), BG_COLOR)

            # --- Layout knobs ---
            left_margin    = int(H * 0.03)
            top_margin     = int(H * 0.03)
            gap_logo_div   = int(H * 0.012)
            gap_div_text   = int(H * 0.012)
            date_time_gap  = int(H * 0.004)

            # --- Date/Time text & metrics ---
            dt = self._now_tz()
            date_str = self._date_text(dt)
            time_str = self._time_text(dt)

            f_date = QFont("Sans"); f_date.setPointSize(int(H * DATE_PT_FRAC))
            f_time = QFont("Sans"); f_time.setPointSize(int(H * TIME_PT_FRAC)); f_time.setBold(True)

            fm_date = QFontMetrics(f_date)
            fm_time = QFontMetrics(f_time)

            date_h = fm_date.height()
            time_h = fm_time.height()
            text_block_h = date_h + date_time_gap + time_h

            # --- Logo scaled to EXACTLY text_block_h ---
            logo_w = 0
            if self.logo:
                pm = self.logo
                pm_draw = pm.scaledToHeight(text_block_h, Qt.SmoothTransformation)
                p.drawPixmap(left_margin, top_margin, pm_draw)
                logo_w = pm_draw.width()

            # --- Divider ---
            divider_x   = left_margin + logo_w + gap_logo_div
            divider_top = top_margin
            divider_bot = top_margin + text_block_h
            pen = QPen(HILITE, 2)
            p.setPen(pen)
            p.drawLine(divider_x, divider_top, divider_x, divider_bot)

            # --- Date + Time block ---
            text_x = divider_x + gap_div_text
            date_y = top_margin + fm_date.ascent()

            ideal_time_y = date_y + date_h + date_time_gap + fm_time.ascent()
            nudge = int(H * TIME_NUDGE_UP_FRAC)
            min_gap = max(2, int(H * 0.002))
            min_allowed_time_y = date_y + fm_date.descent() + min_gap + fm_time.ascent()
            time_y = max(min_allowed_time_y, ideal_time_y - nudge)

            p.setFont(f_date); p.setPen(TEXT_MAIN)
            p.drawText(QPointF(text_x, date_y), date_str)

            p.setFont(f_time); p.setPen(TEXT_MAIN)
            p.drawText(QPointF(text_x, time_y), time_str)

            # --- Header bottom for laying out buttons underneath ---
            header_bottom = divider_bot

            # ----- Buttons (always drawn; only "active" style in root mode) -----
            section_top = header_bottom + int(H * BTN_TOP_PAD_FR)
            base_x = left_margin
            col_w  = int(W * BTN_COL_W_FRAC)
            gap_y  = int(H * BTN_GAP_Y_FRAC)

            base_pt = max(10, int(H * BTN_PT_FRAC))
            f_item_base = QFont("Sans");  f_item_base.setPointSize(base_pt)
            f_item_active = QFont("Sans"); f_item_active.setPointSize(int(round(base_pt * ACTIVE_SCALE))); f_item_active.setBold(True)

            y = section_top
            for i, label in enumerate(self.items):
                p.setFont(f_item_active if (i == self.sel and self.mode=="root") else f_item_base)
                fm = QFontMetrics(p.font())
                baseline = y + fm.ascent()
                p.setPen(TEXT_MAIN)
                p.drawText(QPointF(base_x, baseline), label)
                y += fm.height() + gap_y

            # ----- Guide pane (under current elements) -----
            if self.mode == "guide":
                guide_top = y + int(H * 0.02)  # spacing below buttons
                guide_left  = int(W * GUIDE_LEFT_PAD_FR)
                guide_right = W - int(W * GUIDE_RIGHT_PAD_FR)
                guide_w = max(0, guide_right - guide_left)
                guide_bottom = H - int(H * 0.02)
                guide_h = max(0, guide_bottom - guide_top)

                visible = self.guide_rows[self.g_row_offset : self.g_row_offset + self.g_max_visible]
                n_rows = len(visible)
                row_gap = int(H * GUIDE_ROW_GAP_FR)
                row_h = max(1, (guide_h - max(0, n_rows-1)*row_gap) // max(1, n_rows))

                f_row = QFont("Sans"); f_row.setPointSize(max(10, int(H * GUIDE_ROW_PT_FR)))
                f_row_b = QFont("Sans"); f_row_b.setPointSize(max(10, int(H * GUIDE_ROW_PT_FR))); f_row_b.setBold(True)

                cy = guide_top
                for idx, ch in enumerate(visible):
                    abs_row = self.g_row_offset + idx

                    # left channel label
                    p.setFont(f_row_b if abs_row == self.g_sel_row else f_row)
                    fm = QFontMetrics(p.font())
                    label = f"{ch['id']}  {ch['name']}"
                    label_w = min(int(guide_w * 0.22), fm.horizontalAdvance(label) + int(W*0.008))
                    label_x = guide_left
                    label_y = cy + fm.ascent() + max(0, (row_h - fm.height())//2)
                    p.setPen(TEXT_MAIN if abs_row == self.g_sel_row else TEXT_MUTED)
                    p.drawText(QPointF(label_x, label_y), fm.elidedText(label, Qt.ElideRight, label_w))

                    # vertical separator
                    p.setPen(GRID_ACC)
                    p.drawLine(label_x + label_w + 6, cy, label_x + label_w + 6, cy + row_h)

                    # event cells
                    area_x = label_x + label_w + 12
                    area_w = guide_right - area_x
                    events = ch["events"]

                    cell_w = max(int(W * GUIDE_CELL_MIN_W_FR), int(area_w / max(1, min(5, len(events)))))
                    cx = area_x
                    for c_idx, evd in enumerate(events):
                        cell_rect = QRectF(cx, cy, min(cell_w, area_w - (cx - area_x)), row_h)
                        active = (abs_row == self.g_sel_row and c_idx == self.g_sel_col)

                        if active:
                            p.fillRect(cell_rect, QColor(255,255,255,20))
                            pen = QPen(HILITE, 2); p.setPen(pen); p.drawRect(cell_rect)
                        else:
                            pen = QPen(GRID_ACC, 1); p.setPen(pen); p.drawRect(cell_rect)

                        p.setFont(f_row_b if active else f_row)
                        fm2 = QFontMetrics(p.font())
                        title = evd.get("title","")
                        start_t = datetime.fromtimestamp(evd.get("start",0), tz=self.tz) if self.tz else datetime.fromtimestamp(evd.get("start",0))
                        end_t   = datetime.fromtimestamp(evd.get("end",0), tz=self.tz) if self.tz else datetime.fromtimestamp(evd.get("end",0))
                        timestr = f"{start_t.strftime('%H:%M')}–{end_t.strftime('%H:%M')}"
                        time_y = cell_rect.top() + fm2.ascent() + 6
                        title_y = time_y + fm2.height()
                        p.setPen(TEXT_MUTED)
                        p.drawText(QPointF(cell_rect.left()+8, time_y), fm2.elidedText(timestr, Qt.ElideRight, int(cell_rect.width()-16)))
                        p.setPen(TEXT_MAIN if active else TEXT_MUTED)
                        p.drawText(QPointF(cell_rect.left()+8, title_y), fm2.elidedText(title, Qt.ElideRight, int(cell_rect.width()-16)))

                        cx += cell_w + 6
                        if cx > area_x + area_w - 10:
                            break

                    cy += row_h + row_gap

        finally:
            p.end()

# -------------------- CLI --------------------
def parse_args():
    ap = argparse.ArgumentParser(description="FSX fullscreen menu with PiP preview, looping audio, and inline TV Guide")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9402)
    ap.add_argument("--startup-show", action="store_true", help="Show the menu immediately on start")
    ap.add_argument("--logo", default=str(DEFAULT_LOGO), help="Path to the top-left logo image")
    ap.add_argument("--audio", default=str(DEFAULT_MENU_AUDIO), help="Path to looping menu audio (mp3/ogg)")
    ap.add_argument("--audio-vol", type=int, default=MENU_AUDIO_VOL, help="Menu audio volume 0..100")
    return ap.parse_args()

def main():
    args = parse_args()
    app = QApplication(sys.argv)
    win = MenuWindow(
        logo_path=Path(args.logo),
        audio_path=Path(args.audio),
        audio_vol=args.audio_vol,
    )
    srv = start_http(win, args.host, args.port)
    if args.startup_show:
        win.open_menu()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
