"""Shared presentation for watcher settings; each driver owns its values."""
from PyQt6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox,
                            QGroupBox, QHBoxLayout, QLabel, QSpinBox, QVBoxLayout)


TIMING_FIELDS = (("poll", "Poll", "How often to check for changes."),
                 ("buffer", "Buffer", "Extra delay after the usage limit resets."),
                 ("retry", "Retry", "Delay before retrying a network failure."))


def timing_spin(value, low=1, high=3600):
    spin = QSpinBox()
    spin.setRange(low, high)
    spin.setSuffix(" s")
    spin.setValue(value)
    return spin


def settings_card(title, spins, advanced, extra=(), enabled=None):
    card = QGroupBox(title)
    layout = QVBoxLayout(card)
    layout.setSpacing(6)
    if enabled is not None:
        layout.addWidget(enabled)
    fields = QHBoxLayout()
    fields.setSpacing(8)
    for key, label, tip in TIMING_FIELDS:
        spins[key].setToolTip(spins[key].toolTip() or tip)
        spins[key].setMaximumWidth(110)
        fields.addWidget(QLabel(label))
        fields.addWidget(spins[key])
    for label, widget in extra:
        widget.setMaximumWidth(90)
        fields.addWidget(QLabel(label))
        fields.addWidget(widget)
    fields.addStretch(1)
    fields.addWidget(advanced)
    layout.addLayout(fields)
    return card


class AppSettingsDialog(QDialog):
    """Only settings that apply to Auto-Continue itself, committed on OK."""
    def __init__(self, parent, controls):
        super().__init__(parent)
        self.setWindowTitle("Auto-Continue — App settings")
        self.resize(420, 230)
        layout = QVBoxLayout(self)
        intro = QLabel("These options apply to all selected monitor types.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.checks = {}
        for key, control in controls.items():
            check = QCheckBox(control.text())
            check.setToolTip(control.toolTip())
            check.setChecked(control.isChecked())
            self.checks[key] = check
            layout.addWidget(check)
        layout.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                  | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self):
        return {key: check.isChecked() for key, check in self.checks.items()}
