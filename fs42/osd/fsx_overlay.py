#!/usr/bin/env python3
"""
FSX PySide6 overlay (focusless, input-transparent)
- Corner channel logo (hidden for certain tags)
- FS42-style infobar (95% opacity), with left block + small inline logo + center block
- NEXT = the immediate next event from SQLite (strict EPG behavior, may repeat title)
- Spacing controls: --gap-px and --center-max-frac
- Stacking control: --stack high|low  (use 'low' so the menu can appear above)

Run after fsx_player.py:
  python3 fsx_overlay.py --stack high

Args:
  --host 127.0.0.1
  --port 4243
  --poll-ms 400
  --infobar-ms 2000
  --hide-tags commercial,promo,news
  --gap-px 12
  --center-max-frac 0.62
  --stack high|low
  --debug

Env:
  FSX_ROOT (default ".")
  FSX_TZ   (default "Europe/Brussels")
"""

from __future__ import annotations
import os, sys, json, sqlite3, time, argparse
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF
from PySide6.QtGui  import QGuiApplication, QPainter, QColor, QFont, QPixmap, QPen, QKeyEvent, QFontMetrics
from PySide6.QtWidgets import QApplication, QWidget

# -------------------- Config & Paths --------------------
ROOT      = Path(os.environ.get("FSX_ROOT", ".")).resolve()
CONFS_DIR = (ROOT / "confs").resolve()
SCHED_DB  = (ROOT / "schedules" / "fsx_schedule.sqlite").resolve()
TZ_NAME   = os.environ.get("FSX_TZ", "Europe/Brussels")
TZ        = ZoneInfo(TZ_NAME)

# -------------------- Helpers --------------------
def make_overlay_focusless(w: QWidget, stack: str = "high"):
    """Make a window that never takes focus and ignores all input.

    stack='high' -> harder always-on-top (may cover menu)
    stack='low'  -> softer on-top (menu can rise above)
    """
    w.setAttribute(Qt.WA_TranslucentBackground, True)
    w.setAttribute(Qt.WA_ShowWithoutActivating, True)
    w.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    flags = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
    flags |= Qt.WindowDoesNotAcceptFocus
    if stack == "high":
        try:
            flags |= Qt.BypassWindowManagerHint
        except Exception:
            pass
    try:
        flags |= Qt.WindowTransparentForInput  # Qt ≥ 6.5
    except Exception:
        pass

    w.setWindowFlags(flags)
    w.setFocusPolicy(Qt.NoFocus)
    try:
        w.setAttribute(Qt.WA_X11DoNotAcceptFocus, True)
    except Exception:
        pass

def http_get_json(url: str, timeout: float = 0.5):
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None

def http_post(url: str, timeout: float = 0.5):
    import urllib.request
    try:
        req = urllib.request.Request(url, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return True
    except Exception:
        return False

def load_confs() -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    if not CONFS_DIR.exists():
        return out
    for p in sorted(CONFS_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            sc = data.get("station_conf", {})
            cid = str(sc.get("channel_number") or p.stem)
            sc["_conf_path"] = str(p)
            out[cid] = sc
        except Exception as e:
            print(f"[OVERLAY] bad conf {p.name}: {e}", flush=True)
    return out

def find_logo_path_for_channel(cid: str, confs: Dict[str, Dict[str, Any]]) -> Optional[Path]:
    conf = confs.get(cid) or {}
    cdir = conf.get("content_dir")
    if not cdir:
        return None
    base = (ROOT / cdir / "logos").resolve()
    if not base.exists():
        return None
    dlogo = conf.get("default_logo")
    if dlogo:
        p = (base / dlogo).resolve()
        if p.exists():
            return p
    for ext in (".png", ".jpg", ".jpeg", ".svg", ".webp"):
        for f in sorted(base.glob(f"*{ext}")):
            return f
    return None

def clean_title(t: str) -> str:
    """Strip file extension; keep human title."""
    if not t:
        return ""
    name = Path(t).name
    # remove extension if present
    if "." in name:
        stem = ".".join(name.split(".")[:-1])
    else:
        stem = name
    return stem

def schedule_now_next(conn: sqlite3.Connection, cid: str, now_ts: Optional[int] = None) -> Tuple[Optional[dict], Optional[dict]]:
    """Return (now_row, next_row) for channel_id=cid. NEXT = immediate next by start_utc."""
    if now_ts is None:
        now_ts = int(time.time())
    conn.row_factory = sqlite3.Row
    now_row = conn.execute(
        "SELECT * FROM schedule_events "
        "WHERE channel_id=? AND start_utc<=? AND end_utc>? "
        "ORDER BY start_utc DESC LIMIT 1",
        (cid, now_ts, now_ts)
    ).fetchone()
    next_row = conn.execute(
        "SELECT * FROM schedule_events "
        "WHERE channel_id=? AND start_utc>? "
        "ORDER BY start_utc ASC LIMIT 1",
        (cid, now_ts)
    ).fetchone()
    return (dict(now_row) if now_row else None,
            dict(next_row) if next_row else None)

# -------------------- Overlay --------------------
class FSXOverlay(QWidget):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.confs = load_confs()

        make_overlay_focusless(self, args.stack)

        # Full-screen canvas on primary screen
        screen = QGuiApplication.primaryScreen()
        geom = screen.geometry()
        self.setGeometry(geom)

        # state
        self.current_channel: Optional[str] = None
        self.current_name: str = ""
        self.current_title: str = ""
        self.current_tag: Optional[str] = None
        self.next_title: str = ""
        self.logo_pixmap: Optional[QPixmap] = None     # corner logo
        self.inline_logo: Optional[QPixmap] = None     # infobar inline logo (keep original; scale at paint time)

        # logo prefs
        self.logo_alpha = 0.65
        self.logo_halign = "RIGHT"
        self.logo_valign = "TOP"
        self.logo_margin = 32
        self.logo_max_width_px = max(160, int(geom.width() * 0.18))

        # timers
        self.poll = QTimer(self)
        self.poll.timeout.connect(self.tick)
        self.poll.start(int(self.args.poll_ms))

        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.hide_infobar)

        # infobar visibility + progress
        self._infobar_visible = False
        self._now_start = None
        self._now_end   = None

        self.show()  # will not steal focus

    # ------------- Input fallback: forward keys if we ever receive them -------------
    def keyPressEvent(self, e: QKeyEvent):
        # Normally we never receive keyboard events.
        k = e.key()
        if k in (Qt.Key_Up, Qt.Key_K):
            http_post(f"http://{self.args.host}:{self.args.port}/up")
            e.accept(); return
        if k in (Qt.Key_Down, Qt.Key_J):
            http_post(f"http://{self.args.host}:{self.args.port}/down")
            e.accept(); return
        e.ignore()

    # ------------- Info bar visibility -------------
    def show_infobar(self, ms: int):
        self._infobar_visible = True
        self.hide_timer.start(int(ms))
        self.update()

    def hide_infobar(self):
        self._infobar_visible = False
        self.update()

    # ------------- Periodic tick -------------
    def _apply_logo_prefs_from_conf(self, cid: str):
        sc = self.confs.get(cid) or {}
        self.logo_alpha  = float(sc.get("logo_alpha", self.logo_alpha))
        self.logo_halign = str(sc.get("logo_halign", self.logo_halign)).upper()
        self.logo_valign = str(sc.get("logo_valign", self.logo_valign)).upper()

    def _load_logo_for_channel(self, cid: str):
        p = find_logo_path_for_channel(cid, self.confs)
        if not p:
            self.logo_pixmap = None
            self.inline_logo = None
            return
        pm = QPixmap(str(p))
        if pm.isNull():
            self.logo_pixmap = None
            self.inline_logo = None
            return
        # Corner (full-size constrained)
        corner = pm
        if corner.width() > self.logo_max_width_px:
            corner = corner.scaledToWidth(self.logo_max_width_px, Qt.SmoothTransformation)
        self.logo_pixmap = corner
        # Inline: keep original; scale at paint time to ~70% of bar height
        self.inline_logo = pm

    def _read_status(self) -> Optional[dict]:
        st = http_get_json(f"http://{self.args.host}:{self.args.port}/status")
        if not st: return None
        chan = st.get("channel")
        if chan is None: return None
        name = st.get("name") or st.get("channel_name") or ""
        title = st.get("title") or ""
        return {"channel": str(chan), "name": str(name), "title": str(title)}

    def tick(self):
        st = self._read_status()
        if not st:
            return

        chan = st["channel"]
        fallback_title = st["title"]
        name  = st["name"]

        now_row = None
        next_row = None
        tag_for_now = None

        if SCHED_DB.exists():
            try:
                conn = sqlite3.connect(f"file:{SCHED_DB}?mode=ro", uri=True, check_same_thread=False)
                now_row, next_row = schedule_now_next(conn, chan)
                conn.close()
            except Exception as e:
                if self.args.debug:
                    print(f"[OVERLAY] schedule query failed: {e}", flush=True)

        # NOW strictly from schedule (fallback to player)
        if now_row:
            self.current_title = clean_title(now_row.get("title") or fallback_title)
            self._now_start = int(now_row["start_utc"]) if now_row.get("start_utc") else None
            self._now_end   = int(now_row["end_utc"])   if now_row.get("end_utc")   else None
            tag_for_now     = (now_row.get("tag") or None)
        else:
            self.current_title = clean_title(fallback_title)
            self._now_start = self._now_end = None

        # NEXT = immediate next row (may equal NOW title if schedule repeats)
        self.next_title = clean_title(next_row.get("title") or "") if next_row else ""

        # Channel change?
        if chan != self.current_channel:
            self._apply_logo_prefs_from_conf(chan)
            self._load_logo_for_channel(chan)
            self.current_channel = chan
            self.current_name    = name
            self.show_infobar(self.args.infobar_ms)

        # Tag logic / extra show
        tag_changed = (tag_for_now != self.current_tag)
        self.current_tag = tag_for_now
        if tag_changed and (not self.current_tag or self.current_tag.lower() not in self.args.hide_tags):
            self.show_infobar(int(self.args.infobar_ms * 0.6))

        if self.args.debug:
            print(f"[OVERLAY] NOW={self.current_title!r}, NEXT={self.next_title!r}", flush=True)

        self.update()

    # ------------- Painting -------------
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        W, H = self.width(), self.height()

        # A) Corner channel logo (if not in hidden tag)
        tag = (self.current_tag or "").lower()
        can_show_logo = self.logo_pixmap is not None and (not tag or tag not in self.args.hide_tags)
        if can_show_logo:
            self._paint_corner_logo(p, W, H)

        # B) Infobar
        if self._infobar_visible:
            self._paint_infobar(p, W, H)

        # C) Debug frame
        if self.args.debug:
            self._paint_debug(p, W, H)

        p.end()

    def _paint_corner_logo(self, p: QPainter, W: int, H: int):
        if not self.logo_pixmap:
            return
        pm = self.logo_pixmap
        x = self.logo_margin if self.logo_halign == "LEFT" else (W - pm.width() - self.logo_margin)
        y = self.logo_margin if self.logo_valign == "TOP"  else (H - pm.height() - self.logo_margin)
        p.save()
        p.setOpacity(max(0.0, min(1.0, float(self.logo_alpha))))
        p.drawPixmap(x, y, pm)
        p.restore()

    def _paint_infobar(self, p: QPainter, W: int, H: int):
        # Geometry
        margin   = int(H * 0.02)
        bar_h    = max(96, int(H * 0.20))
        y0       = H - bar_h
        center_y = y0 + bar_h / 2.0

        # Background 95% opacity + top border
        p.save()
        p.setOpacity(0.95)
        p.fillRect(QRectF(0, y0, W, bar_h), QColor("#0a1f3f"))  # dark blue
        p.setOpacity(1.0)
        p.fillRect(QRectF(0, y0, W, 3), QColor("#1a4f98"))      # top line
        p.restore()

        # Layout plan:
        # [Left block (chan no+name)] gap [inline logo] gap [Center block NOW/PB/NEXT]           reserved-right
        gap = max(6, int(self.args.gap_px))
        center_max_w = int(W * float(self.args.center_max_frac))

        # Colors & fonts
        C_TEXT  = QColor("#e9f2ff")
        C_MUTED = QColor("#b8c6e6")
        C_ACC   = QColor("#68a8c3")
        C_TRACK = QColor(255, 255, 255, 90)

        f_chan = QFont("Sans", max(22, int(bar_h * 0.38)));  f_chan.setBold(True)
        f_name = QFont("Sans", max(12, int(bar_h * 0.08)))
        f_now  = QFont("Sans", max(14, int(bar_h * 0.13)))
        f_next = QFont("Sans", max(13, int(bar_h * 0.13)))

        fm_chan = QFontMetrics(f_chan)
        fm_name = QFontMetrics(f_name)
        ch_str  = f"{self.current_channel or ''}"
        name    = self.current_name or ""
        ch_w    = fm_chan.horizontalAdvance(ch_str)
        name_w  = fm_name.horizontalAdvance(name)
        left_block_w = max(ch_w, name_w)
        left_x = margin

        # Left block vertically centered (number above name, right-aligned within block)
        ch_h = fm_chan.height(); name_h = fm_name.height()
        total_left_h = ch_h + max(2, int(bar_h * 0.01)) + name_h
        left_top = center_y - total_left_h / 2.0
        p.setFont(f_chan); p.setPen(C_TEXT)
        p.drawText(QPointF(left_x + (left_block_w - ch_w), left_top + fm_chan.ascent()), ch_str)
        p.setFont(f_name); p.setPen(C_MUTED)
        p.drawText(QPointF(left_x + (left_block_w - name_w), left_top + ch_h + fm_name.ascent()), name)

        # Inline logo (scale to ~70% of bar height, centered vertically)
        cur_x = left_x + left_block_w + gap
        if self.inline_logo and not self.inline_logo.isNull():
            pm = self.inline_logo
            max_h = int(bar_h * 0.70)
            pm_draw = pm.scaledToHeight(max_h, Qt.SmoothTransformation) if pm.height() > max_h else pm
            logo_w, logo_h = pm_draw.width(), pm_draw.height()
            logo_y = int(center_y - logo_h / 2.0)
            p.drawPixmap(cur_x, logo_y, pm_draw)
            cur_x += logo_w + gap

        # Center block (NOW + progress + NEXT), as a vertically-centered group
        center_x = cur_x
        center_w = min(center_max_w, W - center_x - margin - 40)  # reserved right

        # Measure text heights
        fm_now  = QFontMetrics(f_now)
        fm_next = QFontMetrics(f_next)
        now_h   = fm_now.height()
        next_h  = fm_next.height()

        pb_h        = max(4, int(bar_h * 0.06))
        pb_gap_each = int(now_h * 0.25)  # gap from NOW->PB and PB->NEXT (symmetric)

        total_center_h = now_h + pb_gap_each + pb_h + pb_gap_each + next_h
        center_top = int(center_y - total_center_h / 2.0)

        # NOW (top)
        p.setFont(f_now); p.setPen(C_TEXT)
        now_text_raw = self.current_title or ""
        now_text = fm_now.elidedText(now_text_raw, Qt.ElideRight, center_w)
        now_baseline = center_top + fm_now.ascent()
        p.drawText(QPointF(center_x, now_baseline), now_text)

        # Progress bar (middle)
        pb_y = center_top + now_h + pb_gap_each
        p.save(); p.setOpacity(0.9)
        p.fillRect(QRectF(center_x, pb_y, center_w, pb_h), C_TRACK)
        p.restore()

        prog = 0.0
        if self._now_start is not None and self._now_end is not None and self._now_end > self._now_start:
            now_s = int(time.time())
            prog = (now_s - int(self._now_start)) / (int(self._now_end) - int(self._now_start))
            prog = max(0.0, min(1.0, prog))
        p.fillRect(QRectF(center_x, pb_y, int(center_w * prog), pb_h), C_ACC)

        # NEXT (bottom)
        p.setFont(f_next); p.setPen(C_MUTED)
        next_text = fm_next.elidedText(self.next_title or "", Qt.ElideRight, center_w)
        next_baseline = pb_y + pb_h + pb_gap_each + fm_next.ascent()
        p.drawText(QPointF(center_x, next_baseline), next_text)

    def _paint_debug(self, p: QPainter, w: int, h: int):
        p.save()
        pen = QPen(QColor(255, 0, 0, 160), 1)
        p.setPen(pen)
        p.drawLine(w//2, 0, w//2, h)
        p.drawLine(0, h//2, w, h//2)
        p.drawRect(0, 0, w-1, h-1)
        p.restore()

# -------------------- CLI --------------------
def parse_args():
    ap = argparse.ArgumentParser(description="FSX overlay (input-transparent + FS42-styled infobar)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4243)
    ap.add_argument("--poll-ms", type=int, default=400)
    ap.add_argument("--infobar-ms", type=int, default=2000)
    ap.add_argument("--hide-tags", default="commercial,promo,news")
    ap.add_argument("--gap-px", type=int, default=12)
    ap.add_argument("--center-max-frac", type=float, default=0.62)
    ap.add_argument("--stack", choices=["high","low"], default="high")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    args.hide_tags = set(t.strip().lower() for t in str(args.hide_tags).split(",") if t.strip())
    return args

def main():
    args = parse_args()
    app = QApplication(sys.argv)
    overlay = FSXOverlay(args)
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
