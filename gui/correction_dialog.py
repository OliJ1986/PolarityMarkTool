"""
gui/correction_dialog.py
────────────────────────
Dialog for manually correcting a component's polarity marking.

Fields
──────
• Polar status  : Auto (ODB++ heuristic) | Force polar | Force non-polar
• Flip pin      : swap which pin is marked (e.g. pin1↔pin2 for diodes)
• Note          : free text comment
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QCheckBox, QDialogButtonBox,
    QGroupBox, QRadioButton, QButtonGroup, QSizePolicy,
)


class CorrectionDialog(QDialog):
    """Edit the manual polarity override for one component."""

    def __init__(self, ref: str, comp_type: str,
                 current: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Polarity correction — {ref}")
        self.setMinimumWidth(380)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # ── Info header ───────────────────────────────────────────────────
        hdr = QLabel(f"<b>{ref}</b>  <i>({comp_type})</i>")
        hdr.setAlignment(Qt.AlignCenter)
        layout.addWidget(hdr)

        # ── Polar-status group ────────────────────────────────────────────
        grp = QGroupBox("Polar status")
        grp_layout = QVBoxLayout(grp)

        self._rb_auto   = QRadioButton("Auto  (use ODB++ / heuristic detection)")
        self._rb_polar  = QRadioButton("Force  polar  — always show marker")
        self._rb_none   = QRadioButton("Force  non-polar  — never show marker")
        self._rb_group  = QButtonGroup(self)
        for rb in (self._rb_auto, self._rb_polar, self._rb_none):
            self._rb_group.addButton(rb)
            grp_layout.addWidget(rb)

        # Set initial state
        force_polar = current.get("polar")
        if force_polar is True:
            self._rb_polar.setChecked(True)
        elif force_polar is False:
            self._rb_none.setChecked(True)
        else:
            self._rb_auto.setChecked(True)

        layout.addWidget(grp)

        # ── Flip pin checkbox ─────────────────────────────────────────────
        self._flip_cb = QCheckBox(
            "Flip pin  — mark the other pin instead of the auto-detected one\n"
            "(e.g. pin 1 instead of pin 2, or vice versa)"
        )
        self._flip_cb.setChecked(bool(current.get("flip_pin", False)))
        layout.addWidget(self._flip_cb)

        # ── Note field ────────────────────────────────────────────────────
        note_layout = QFormLayout()
        self._note_edit = QLineEdit(current.get("note", ""))
        self._note_edit.setPlaceholderText("Optional comment …")
        note_layout.addRow("Note:", self._note_edit)
        layout.addLayout(note_layout)

        # ── Buttons ───────────────────────────────────────────────────────
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ── Result ────────────────────────────────────────────────────────────

    def get_correction(self) -> dict:
        """Return the correction dict to store.  Empty dict = no override."""
        result: dict = {}
        if self._rb_polar.isChecked():
            result["polar"] = True
        elif self._rb_none.isChecked():
            result["polar"] = False
        # else: "auto" → no "polar" key in dict
        if self._flip_cb.isChecked():
            result["flip_pin"] = True
        note = self._note_edit.text().strip()
        if note:
            result["note"] = note
        return result

