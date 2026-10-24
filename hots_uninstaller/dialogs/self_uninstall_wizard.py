from PySide6.QtWidgets import QVBoxLayout, QHBoxLayout, QLabel, QFrame

from ..icons import FIF
from ..constants import DARK
from ..widgets_qt import HOTSDialog, HOTSButton, HOTSSwitch, h_separator
from ..i18n import T


class _OptionCard(QFrame):
    def __init__(self, title: str, desc: str, default: bool, enabled: bool, parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            f"QFrame {{ background-color: {DARK['btn_bg']}; border: 1px solid {DARK['border']}; "
            f"border-radius: 8px; }}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(3)

        self.switch = HOTSSwitch(title, font_pt=9.5, spacing=10)
        self.switch.setChecked(bool(default and enabled))
        self.switch.setEnabled(enabled)
        lay.addWidget(self.switch)

        desc_lbl = QLabel(desc)
        desc_lbl.setWordWrap(True)
        desc_lbl.setStyleSheet(
            f"color: {DARK['fg2']}; font-size: 8.3pt; background: transparent; "
            f"border: none; padding-left: 44px;"
        )
        lay.addWidget(desc_lbl)

    def is_checked(self) -> bool:
        return self.switch.isChecked()


class SelfUninstallWizardDialog(HOTSDialog):
    def __init__(self, parent=None, counts: dict | None = None):
        super().__init__(parent, T("uninst_wizard_title"), min_width=420, min_height=140)
        self._counts = counts or {}
        self.choices = None
        self._build()
        self.adjustSize()
        self.center_on_parent()

    def _build(self):
        cl = self.body_layout
        cl.setContentsMargins(28, 22, 28, 20)
        cl.setSpacing(10)

        heading = QLabel(T("uninst_wizard_heading"))
        heading.setStyleSheet(
            f"color: {DARK['fg']}; font-size: 11pt; font-weight: 600; "
            f"background: transparent; border: none;"
        )
        cl.addWidget(heading)

        intro = QLabel(T("uninst_wizard_intro"))
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {DARK['fg2']}; font-size: 9pt; background: transparent; border: none;")
        cl.addWidget(intro)

        cl.addSpacing(6)
        cl.addWidget(h_separator())
        cl.addSpacing(6)

        n_items = self._counts.get("saved_items", 0)
        self._card_data = _OptionCard(
            T("uninst_opt_data_title"),
            T("uninst_opt_data_desc_n", n=n_items) if n_items else T("uninst_opt_data_desc_none"),
            default=True, enabled=True,
        )
        cl.addWidget(self._card_data)

        cl.addSpacing(2)
        footer = QLabel(T("uninst_wizard_footer"))
        footer.setWordWrap(True)
        footer.setStyleSheet(f"color: {DARK['muted_fg']}; font-size: 8pt; background: transparent; border: none;")
        cl.addWidget(footer)

        cl.addStretch()
        cl.addWidget(h_separator())

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 12, 0, 0)
        btn_row.addStretch()

        confirm_btn = HOTSButton(FIF.DELETE, DARK['red'], T("uninst_btn_confirm"))
        confirm_btn.fit_to_content()
        confirm_btn.clicked.connect(self._confirm)
        btn_row.addWidget(confirm_btn)

        cancel_btn = HOTSButton(FIF.CLOSE, DARK['fg'], T("btn_cancel"))
        cancel_btn.fit_to_content()
        cancel_btn.clicked.connect(self.reject)
        btn_row.addSpacing(10)
        btn_row.addWidget(cancel_btn)

        btn_row.addStretch()

        cl.addLayout(btn_row)

    def _confirm(self):
        self.choices = {
            "delete_saved_data": self._card_data.is_checked(),
        }
        self.accept()
