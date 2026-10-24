from typing import Optional

from PySide6.QtWidgets import QVBoxLayout, QHBoxLayout, QWidget, QLabel, QDialog
from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap

from ..icons import FIF
from ..constants import DARK, hex_to_rgb
from ..widgets_qt import HOTSDialog, HOTSButton
from ..i18n import T


def _make_trash_icon_label(size: int, color: str, crossed: bool) -> QLabel:
    lbl = QLabel()
    lbl.setFixedSize(size, size)
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    qcolor = QColor(color)
    painter = QPainter(pm)
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(qcolor)
        pen.setWidthF(3)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        margin = 2.0
        painter.drawEllipse(QRectF(margin, margin, size - 2 * margin, size - 2 * margin))

        icon_size = size * 0.42
        icon_rect = QRectF((size - icon_size) / 2, (size - icon_size) / 2, icon_size, icon_size)
        FIF.DELETE.paint(painter, icon_rect, qcolor)

        if crossed:
            cross_pen = QPen(qcolor)
            cross_pen.setWidthF(3)
            cross_pen.setCapStyle(Qt.RoundCap)
            painter.setPen(cross_pen)
            inset = size * 0.24
            painter.drawLine(QPointF(inset, size - inset), QPointF(size - inset, inset))
    finally:
        painter.end()
    lbl.setPixmap(pm)
    lbl.setAlignment(Qt.AlignCenter)
    return lbl


class ConfirmDialog(HOTSDialog):
    def __init__(self, parent, title: str, message: str, note: Optional[str] = None,
                 permanent: bool = True, confirm_label: Optional[str] = None,
                 min_width: int = 380, min_height: int = 260):
        super().__init__(parent, title, min_width=min_width, min_height=min_height)
        self._build_body(message, note, permanent, confirm_label)

    def _build_body(self, message: str, note: Optional[str], permanent: bool,
                     confirm_label: Optional[str]):
        lay = QVBoxLayout()
        lay.setContentsMargins(28, 28, 28, 20)
        lay.setSpacing(12)
        lay.addStretch(1)

        color = DARK["red"] if permanent else DARK["accent"]
        lay.addWidget(_make_trash_icon_label(72, color, crossed=permanent), 0, Qt.AlignCenter)

        msg_lbl = QLabel(message)
        msg_lbl.setAlignment(Qt.AlignCenter)
        msg_lbl.setWordWrap(True)
        msg_lbl.setStyleSheet(f"color: {DARK['fg']}; font-size: 10pt; background: transparent;")
        lay.addWidget(msg_lbl)

        if note:
            note_lbl = QLabel(note)
            note_lbl.setAlignment(Qt.AlignCenter)
            note_lbl.setWordWrap(True)
            if permanent:
                r, g, b = hex_to_rgb(DARK["red"])
                note_lbl.setStyleSheet(
                    f"color: {DARK['fg']}; font-size: 8.5pt; background: rgba({r},{g},{b},0.12); "
                    f"border: 1px solid rgba({r},{g},{b},0.35); border-radius: 6px; padding: 8px 12px;"
                )
            else:
                note_lbl.setStyleSheet(
                    f"color: {DARK['fg2']}; font-size: 8.5pt; background: {DARK['border_soft']}; "
                    f"border: 1px solid {DARK['border_soft2']}; border-radius: 6px; padding: 8px 12px;"
                )
            lay.addWidget(note_lbl)

        lay.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        label = confirm_label or (T("btn_delete") if permanent else T("btn_move_to_recycle"))
        yes_btn = HOTSButton(FIF.DELETE, color, label, accent=True)
        yes_btn.fit_to_content(min_width=130)
        yes_btn.clicked.connect(self.accept)
        btn_row.addWidget(yes_btn)

        no_btn = HOTSButton(FIF.CLOSE, DARK["fg2"], T("btn_cancel"))
        no_btn.fit_to_content(min_width=110)
        no_btn.clicked.connect(self.reject)
        btn_row.addWidget(no_btn)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)

        self.body_layout.addLayout(lay)

    @staticmethod
    def ask(parent, title: str, message: str, note: Optional[str] = None,
            permanent: bool = True, confirm_label: Optional[str] = None) -> bool:
        d = ConfirmDialog(parent, title, message, note=note, permanent=permanent,
                           confirm_label=confirm_label)
        return d.exec() == QDialog.Accepted


class SummaryDialog(HOTSDialog):
    def __init__(self, parent, title: str, message: str, cards: list,
                 note: Optional[str] = None, min_width: int = 380, min_height: int = 260):
        super().__init__(parent, title, min_width=min_width, min_height=min_height)
        self._build_body(message, cards, note)

    def _card_style(self) -> str:
        return (f"background: {DARK['panel_bg']}; "
                f"border: 1px solid {DARK['border_faint']}; border-radius: 6px;")

    def _build_body(self, message: str, cards: list, note: Optional[str]):
        lay = QVBoxLayout()
        lay.setContentsMargins(28, 28, 28, 20)
        lay.setSpacing(12)
        lay.addStretch(1)

        check_lbl = QLabel("✓")
        check_lbl.setAlignment(Qt.AlignCenter)
        check_lbl.setFixedSize(72, 72)
        check_lbl.setStyleSheet(
            f"color: {DARK['green']}; font-size: 28pt; font-weight: 600; background: transparent; "
            f"border: 3px solid {DARK['green']}; border-radius: 36px;"
        )
        lay.addWidget(check_lbl, 0, Qt.AlignCenter)

        msg_lbl = QLabel(message)
        msg_lbl.setAlignment(Qt.AlignCenter)
        msg_lbl.setWordWrap(True)
        msg_lbl.setStyleSheet(f"color: {DARK['fg']}; font-size: 10pt; background: transparent;")
        lay.addWidget(msg_lbl)

        if cards:
            cards_row = QHBoxLayout()
            cards_row.setSpacing(12)
            cards_row.addStretch(1)
            for value_text, label_text in cards:
                cards_row.addWidget(self._make_stat_card(value_text, label_text))
            cards_row.addStretch(1)
            lay.addLayout(cards_row)

        if note:
            note_lbl = QLabel(note)
            note_lbl.setAlignment(Qt.AlignCenter)
            note_lbl.setWordWrap(True)
            note_lbl.setStyleSheet(f"color: {DARK['fg2']}; font-size: 8.5pt; background: transparent;")
            lay.addWidget(note_lbl)

        lay.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        ok_btn = HOTSButton(FIF.ACCEPT, DARK["accent"], T("btn_ok"), accent=True)
        ok_btn.fit_to_content(min_width=110)
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)

        self.body_layout.addLayout(lay)

    def _make_stat_card(self, value_text: str, label_text: str) -> QWidget:
        card = QWidget()
        card.setFixedWidth(150)
        card.setStyleSheet(self._card_style())
        v = QVBoxLayout(card)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(2)
        value_lbl = QLabel(value_text)
        value_lbl.setAlignment(Qt.AlignCenter)
        value_lbl.setStyleSheet(
            f"color: {DARK['accent']}; font-size: 15pt; font-weight: 600; background: transparent; border: none;"
        )
        title_lbl = QLabel(label_text)
        title_lbl.setAlignment(Qt.AlignCenter)
        title_lbl.setWordWrap(True)
        title_lbl.setStyleSheet(f"color: {DARK['fg2']}; font-size: 8pt; background: transparent; border: none;")
        v.addWidget(value_lbl)
        v.addWidget(title_lbl)
        return card
