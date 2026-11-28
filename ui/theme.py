"""Shared colors and the Qt stylesheet for FileBot's popover and avatar."""
from __future__ import annotations

PALETTE = {
    "bg": "#0f1117",
    "panel": "#161923",
    "surface": "#1e2230",
    "surface_hi": "#272c3e",
    "border": "#2c3247",
    "text": "#eef0f7",
    "muted": "#98a0b8",
    "accent": "#7c6cf0",
    "accent_hi": "#9488ff",
    "ok": "#34d399",
    "warn": "#f5a524",
    "danger": "#f4606c",
}

# File-type badge colors used by the results list.
EXT_COLORS = {
    ".pdf": "#e5484d", ".docx": "#3b82f6", ".doc": "#3b82f6", ".xlsx": "#22a06b",
    ".csv": "#22a06b", ".pptx": "#f08a24", ".md": "#8b9dff", ".txt": "#8892a8",
    ".html": "#e5833d", ".htm": "#e5833d", ".json": "#d4b43a", ".py": "#4f9bd8",
    ".js": "#d4b43a", ".ts": "#3b82f6",
}
DEFAULT_EXT_COLOR = "#6b7390"

STATUS_PILLS = {
    "sleep": ("Sleeping", PALETTE["muted"]),
    "active": ("Awake", PALETTE["ok"]),
    "working": ("Working…", PALETTE["accent_hi"]),
    "cmd_pending": ("Needs approval", PALETTE["warn"]),
}


def stylesheet() -> str:
    p = PALETTE
    return f"""
    QWidget {{ color:{p['text']}; font:10pt 'Segoe UI','Inter','Noto Sans',sans-serif; }}
    QFrame#panel {{ background:{p['panel']}; border:1px solid {p['border']}; border-radius:16px; }}
    QLabel {{ background:transparent; border:0; }}
    QLabel#title {{ font-weight:700; font-size:12pt; }}
    QLabel#subtitle {{ color:{p['muted']}; font-size:8.5pt; }}
    QLabel#pill {{ border-radius:9px; padding:2px 10px; font-size:8.5pt; font-weight:600;
                   background:{p['surface']}; }}
    QLabel#chip {{ color:{p['muted']}; background:{p['surface']}; border-radius:8px;
                   padding:2px 8px; font-size:8pt; }}
    QLabel#section {{ color:{p['muted']}; font-size:8pt; font-weight:600; letter-spacing:1px; }}
    QLineEdit {{ background:{p['surface']}; border:1px solid {p['border']}; border-radius:12px;
                 padding:9px 12px; selection-background-color:{p['accent']}; }}
    QLineEdit:focus {{ border:1px solid {p['accent']}; }}
    QLineEdit:disabled {{ color:{p['muted']}; }}
    QPushButton {{ background:{p['surface_hi']}; border:1px solid {p['border']}; border-radius:10px;
                   padding:7px 14px; font-weight:600; }}
    QPushButton:hover {{ border:1px solid {p['accent']}; }}
    QPushButton:disabled {{ color:{p['muted']}; background:{p['surface']}; }}
    QPushButton#primary {{ background:{p['accent']}; border:0; color:white; }}
    QPushButton#primary:hover {{ background:{p['accent_hi']}; }}
    QPushButton#primary:disabled {{ background:#3a3560; color:#a39fc8; }}
    QPushButton#approve {{ background:{p['ok']}; color:#06241a; border:0; }}
    QPushButton#reject {{ background:transparent; border:1px solid {p['danger']}; color:{p['danger']}; }}
    QListWidget {{ background:transparent; border:0; outline:0; }}
    QListWidget::item {{ background:transparent; border:0; }}
    QTextEdit {{ background:{p['surface']}; border:1px solid {p['border']}; border-radius:12px;
                 padding:8px 10px; selection-background-color:{p['accent']}; }}
    QFrame#cmdcard {{ background:#2a2217; border:1px solid #5b4420; border-radius:12px; }}
    QScrollBar:vertical {{ background:transparent; width:8px; margin:2px; }}
    QScrollBar::handle:vertical {{ background:{p['border']}; border-radius:4px; min-height:24px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
    """
