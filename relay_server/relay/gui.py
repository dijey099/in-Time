"""Qt (PySide6) window of the relay server."""
import html
from datetime import date, datetime

import qrcode
from PySide6.QtCore import QDate, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QDateEdit, QFrame, QHBoxLayout, QHeaderView, QLabel,
                               QMainWindow, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from .config import bundle_dir
from .sync import ERROR, REACHABLE, UNREACHABLE

# Palette from the in-Time logo. Accents only: backgrounds and text follow the system theme.
BRAND_SKY = "#0A91FA"     # light end of the logo gradient (readable on dark and light themes)
BRAND = "#0067F2"
BRAND_DEEP = "#0050E0"
INDIGO_LIGHT = "#7C5CFF"  # lighter tint of the logo indigo #2E14A0, readable on dark themes
LOGO_PATH = bundle_dir() / "relay" / "assets" / "logo.png"  # bundled with --add-data in builds

STATUS_COLORS = {REACHABLE: "#16a34a", UNREACHABLE: "#d97706", ERROR: "#dc2626"}
SYNC_COLORS = {"synced": BRAND_SKY, "pending": "#9ca3af", "rejected": "#dc2626"}

PRIMARY_BUTTON_QSS = f"""
QPushButton#primary {{
    color: white; font-weight: bold; border: 0; border-radius: 6px; padding: 6px 16px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {BRAND_SKY}, stop:0.55 {BRAND}, stop:1 {BRAND_DEEP});
}}
QPushButton#primary:hover {{ background: {BRAND_SKY}; }}
QPushButton#primary:pressed {{ background: {BRAND_DEEP}; }}
"""


def logo_tile(size: int, ratio: float = 1.0) -> QPixmap:
    """Logo on a white rounded tile, so its indigo parts stay visible on dark themes."""
    px = round(size * ratio)
    pm = QPixmap(px, px)
    pm.fill(Qt.transparent)
    logo = QPixmap(str(LOGO_PATH))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, px, px), px * 0.22, px * 0.22)
    p.fillPath(path, QColor("white"))
    if not logo.isNull():
        inner = round(px * 0.8)
        off = (px - inner) // 2
        p.drawPixmap(off, off, logo.scaled(inner, inner, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    p.end()
    pm.setDevicePixelRatio(ratio)
    return pm


def qr_pixmap(text: str, size: int = 150) -> QPixmap:
    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)
    pm = QPixmap(size, size)
    pm.fill(Qt.white)
    p = QPainter(pm)
    cell = size / n
    p.setPen(Qt.NoPen)
    p.setBrush(Qt.black)
    for y, row in enumerate(matrix):
        for x, on in enumerate(row):
            if on:
                p.drawRect(QRectF(x * cell, y * cell, cell + 0.5, cell + 0.5))
    p.end()
    return pm


def cloud_icon(color: str, crossed=False) -> QIcon:
    """Small cloud icon: blue = synced with Main server, grey = waiting, red = rejected."""
    pm = QPixmap(28, 20)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(QRectF(2, 8, 10, 10))
    p.drawEllipse(QRectF(7, 3, 12, 12))
    p.drawEllipse(QRectF(15, 7, 11, 11))
    p.drawRect(QRectF(7, 12, 14, 6))
    if crossed:
        p.setPen(QColor("white"))
        p.drawLine(10, 8, 18, 16)
        p.drawLine(18, 8, 10, 16)
    p.end()
    return QIcon(pm)


class MainWindow(QMainWindow):
    COLS = ["Time", "Timezone", "Photo", "ID", "Full name", "Relay", "Direction", "Status", "Main server"]

    def __init__(self, cfg, db, sync, phone_urls, models=None, clock=None):
        super().__init__()
        self.cfg, self.db, self.sync, self.models, self.clock = cfg, db, sync, models, clock
        self._following_today = True  # day selector follows the trusted date
        self._last_signature = None
        self._photo_cache = {}
        self.setWindowTitle("in-Time relay")
        if LOGO_PATH.exists():
            self.setWindowIcon(QIcon(str(LOGO_PATH)))
        self.setStyleSheet(PRIMARY_BUTTON_QSS)
        self.resize(1100, 700)

        root = QWidget()
        layout = QVBoxLayout(root)
        self.setCentralWidget(root)

        # ---- blocking banner when there is no internet (no trusted time)
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet("background:#dc2626; color:white; font-size:14pt; font-weight:bold;"
                                  "padding:10px 14px; border-radius:6px;")
        self.banner.hide()
        layout.addWidget(self.banner)

        # ---- header: site + main server status + phone address
        header = QHBoxLayout()
        left = QVBoxLayout()
        self.site_label = QLabel("in-Time relay")
        f = QFont()
        f.setPointSize(18)
        f.setBold(True)
        self.site_label.setFont(f)
        self.site_label.setTextFormat(Qt.RichText)
        self._set_title("")
        title_row = QHBoxLayout()
        title_row.setSpacing(12)
        logo = QLabel()
        logo.setPixmap(logo_tile(48, self.devicePixelRatioF()))
        logo.setFixedSize(48, 48)
        logo.setToolTip("in-Time")
        title_row.addWidget(logo)
        title_row.addWidget(self.site_label, 1)
        self.clock_label = QLabel()
        cf = QFont()
        cf.setPointSize(18)
        cf.setBold(True)
        self.clock_label.setFont(cf)
        self.clock_label.setToolTip("Trusted time (from the internet), not the PC clock")
        left.addLayout(title_row)

        net_row = QHBoxLayout()
        self.net_dot = QLabel()
        self.net_dot.setFixedSize(16, 16)
        self.net_label = QLabel()
        self.net_label.setTextFormat(Qt.RichText)
        net_row.addWidget(QLabel("Internet:"))
        net_row.addWidget(self.net_dot)
        net_row.addWidget(self.net_label, 1)
        left.addLayout(net_row)

        status_row = QHBoxLayout()
        self.status_dot = QLabel()
        self.status_dot.setFixedSize(16, 16)
        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.RichText)
        status_row.addWidget(QLabel("Main server:"))
        status_row.addWidget(self.status_dot)
        status_row.addWidget(self.status_label, 1)
        left.addLayout(status_row)
        self.sync_label = QLabel()
        self.sync_label.setWordWrap(True)
        self.sync_label.setStyleSheet("color:#6b7280")
        left.addWidget(self.sync_label)

        btns = QHBoxLayout()
        sync_btn = QPushButton("Sync now")
        sync_btn.setObjectName("primary")
        sync_btn.setCursor(Qt.PointingHandCursor)
        sync_btn.clicked.connect(self.sync.wake)
        btns.addWidget(sync_btn)
        self.retry_btn = QPushButton("Retry rejected")
        self.retry_btn.clicked.connect(self._retry_rejected)
        btns.addWidget(self.retry_btn)
        btns.addStretch(1)
        left.addLayout(btns)
        left.addStretch(1)
        header.addLayout(left, 1)

        phone_box = QFrame()
        phone_box.setFrameShape(QFrame.StyledPanel)
        pl = QHBoxLayout(phone_box)
        qr = QLabel()
        qr.setPixmap(qr_pixmap(phone_urls[0]))
        pl.addWidget(qr)
        txt = QLabel("Scan the code or open:<br>" +
                     "<br>".join(f"<span style='font-size:14pt'>{u}</span>" for u in phone_urls) +
                     "<br><small>Accept the certificate warning the first time.</small>")
        txt.setTextInteractionFlags(Qt.TextSelectableByMouse)
        right = QVBoxLayout()  # clock on top of the "Scan the code" text
        right.addWidget(self.clock_label)
        right.addWidget(txt)
        right.addStretch(1)
        pl.addLayout(right)
        header.addWidget(phone_box)
        layout.addLayout(header)

        # ---- day selector + counters
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Day:"))
        self.day = QDateEdit(self._today_qdate())
        self.day.setDisplayFormat("dd/MM/yyyy")
        self.day.setCalendarPopup(True)
        self.day.dateChanged.connect(self._day_changed)
        bar.addWidget(self.day)
        today_btn = QPushButton("Today")
        today_btn.clicked.connect(lambda: self.day.setDate(self._today_qdate()))
        bar.addWidget(today_btn)
        bar.addSpacing(20)
        self.counters = QLabel()
        bar.addWidget(self.counters, 1)
        layout.addLayout(bar)

        # ---- check-ins table
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(52)
        self.table.setIconSize(QSize(44, 44))
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh(force=True)

    # ------------------------------------------------------------------
    def _set_title(self, site):
        title = f"<span style='color:{BRAND_SKY}'>in-Time</span> relay"
        self.site_label.setText(f"{title} · {html.escape(site)}" if site else title)

    def _today_qdate(self):
        today = self.clock.today() if self.clock else None
        return QDate(today.year, today.month, today.day) if today else QDate.currentDate()

    def _day_changed(self, _):
        self._following_today = self.day.date() == self._today_qdate()
        self.refresh(force=True)

    def _refresh_clock(self):
        if not self.clock:
            return
        st = self.clock.status()
        now = self.clock.now()
        tz = html.escape(self.cfg.tz_name)
        if now:
            self.clock_label.setText(now.strftime("%H:%M:%S"))
            self.clock_label.setStyleSheet("")  # theme text color: light on dark themes, dark on light ones
            self.net_dot.setStyleSheet("background:#16a34a; border-radius:8px;")
            self.net_label.setText(f"<b style='color:#16a34a'>OK</b> — {tz}")
            self.banner.hide()
            # follow midnight when the selector shows "today"
            if self._following_today and self.day.date() != self._today_qdate():
                self.day.setDate(self._today_qdate())
        else:
            self.clock_label.setText("--:--:--")
            self.clock_label.setStyleSheet("color:#dc2626")
            self.net_dot.setStyleSheet("background:#dc2626; border-radius:8px;")
            self.net_label.setText(f"<b style='color:#dc2626'>No connection</b> — {html.escape(st['message'])}")
            if "date of this PC" in st["message"]:
                self.banner.setText("The date of this PC is wrong, so the internet time can't be verified — "
                                    "new check-ins are refused. Fix the date/time in Windows.")
            else:
                self.banner.setText("No internet connection — new check-ins are refused until it is back "
                                    "(checked every 10 s). Check the network cable / Wi-Fi of this PC.")
            self.banner.show()
        warnings = []
        if st["pc_offset"] is not None and abs(st["pc_offset"]) > 120:
            mins = st["pc_offset"] / 60
            warnings.append(f"PC clock is {abs(mins):.0f} min {'ahead' if mins > 0 else 'behind'} "
                            f"(ignored: check-ins use the internet time)")
        if self.cfg.tz_from_pc:
            warnings.append(f"Time zone taken from this PC ({tz}): set [time] timezone in relay.ini")
        self._clock_warnings = warnings

    def _retry_rejected(self):
        self.db.retry_rejected()
        self.sync.wake()

    def _photo(self, user_id):
        if user_id not in self._photo_cache:
            u = self.db.user(user_id)
            pm = QPixmap()
            if u and u["photo_file"]:
                pm.load(str(self.cfg.photos_dir / u["photo_file"]))
            self._photo_cache[user_id] = QIcon(pm.scaled(44, 44, Qt.KeepAspectRatioByExpanding,
                                                         Qt.SmoothTransformation)) if not pm.isNull() else QIcon()
        return self._photo_cache[user_id]

    def refresh(self, force=False):
        self._clock_warnings = []
        self._refresh_clock()
        st = self.sync.state.snapshot()
        color = STATUS_COLORS.get(st["status"], "#9ca3af")
        self.status_dot.setStyleSheet(f"background:{color}; border-radius:8px;")
        self.status_label.setText(f"<b style='color:{color}'>{st['status']}</b> — {st['message']}")
        if st["site_name"]:
            self._set_title(st["site_name"])
        last_ok = st["last_ok"].strftime("%d/%m/%Y %H:%M:%S") if st["last_ok"] else "never"
        settings = self.db.get("settings", {})
        self.sync_label.setText(
            f"Last successful sync: {last_ok}   ·   Max in time: {settings.get('max_in_time', '?')}"
            f"   ·   Min out time: {settings.get('min_out_time', '?')}   ·   {len(self.db.users())} users cached")
        for w in self._clock_warnings:
            self.sync_label.setText(self.sync_label.text() + f"<br><b style='color:#d97706'>{w}</b>")
        for check in self.models or []:  # face models (phone page, relay recognition)
            if not check.ok:
                self.sync_label.setText(self.sync_label.text()
                                        + f"<br><b style='color:#dc2626'>{html.escape(check.message)}</b>")
            elif getattr(check, "warning", ""):
                self.sync_label.setText(self.sync_label.text()
                                        + f"<br><b style='color:#d97706'>{html.escape(check.warning)}</b>")

        counts = self.db.counts()
        self.retry_btn.setVisible(bool(counts.get("rejected")))
        d = self.day.date()
        rows = self.db.checkins_for_day(date(d.year(), d.month(), d.day()))
        sig = tuple((r["uuid"], r["sync_state"]) for r in rows)
        if not force and sig == self._last_signature:
            return
        self._last_signature = sig
        self._photo_cache.clear()

        ok = sum(r["status"] == "OK" for r in rows)
        pending_all = counts.get("pending", 0)
        self.counters.setText(f"<b>{len(rows)}</b> check-ins · <span style='color:#16a34a'><b>{ok}</b> OK</span> · "
                              f"<span style='color:#dc2626'><b>{len(rows) - ok}</b> KO</span> · "
                              f"<b>{pending_all}</b> waiting for Main server")

        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            ts = datetime.fromisoformat(r["ts"])
            offset = r["ts"][19:] or ""  # "+03:00" part of the stored time
            items = [
                QTableWidgetItem(ts.strftime("%H:%M:%S")),
                QTableWidgetItem(r["timezone"] if (r["timezone"] or "").startswith("UTC")
                                 else f"{r['timezone'] or '—'} ({offset})"),
                QTableWidgetItem(self._photo(r["user_id"]), ""),
                QTableWidgetItem(r["user_id"]),
                QTableWidgetItem(r["full_name"]),
                QTableWidgetItem(r["relay_name"] or "—"),
                QTableWidgetItem(r["direction"]),
                QTableWidgetItem(r["status"] + (f" ({r['reason']})" if r["reason"] else "")),
                QTableWidgetItem(cloud_icon(SYNC_COLORS[r["sync_state"]], r["sync_state"] == "rejected"),
                                 {"synced": "Synced", "pending": "Not synced",
                                  "rejected": "Rejected"}[r["sync_state"]]),
            ]
            st_item, dir_item, sync_item = items[7], items[6], items[8]
            st_item.setForeground(QColor("#16a34a" if r["status"] == "OK" else "#dc2626"))
            f = st_item.font()
            f.setBold(True)
            st_item.setFont(f)
            dir_item.setForeground(QColor(BRAND_SKY if r["direction"] == "IN" else INDIGO_LIGHT))
            if r["sync_state"] == "rejected":
                sync_item.setToolTip(r["sync_error"] or "")
            elif r["synced_at"]:
                sync_item.setToolTip(f"Synced at {r['synced_at']}")
            for c, it in enumerate(items):
                self.table.setItem(i, c, it)
