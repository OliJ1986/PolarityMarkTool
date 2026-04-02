"""
gui/correction_dialog.py
────────────────────────
Dialog for manually correcting a component's polarity marking.
Supports both single-component and bulk (multi-component) editing.

Fields
──────
• Polar status  : Auto (ODB++ heuristic) | Force polar | Force non-polar
• Flip pin      : swap which pin is marked (e.g. pin1↔pin2 for diodes)
• Note          : free text comment  (single mode only)
"""
from __future__ import annotations

from typing import Dict, List, Union

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QCheckBox, QDialogButtonBox,
    QGroupBox, QRadioButton, QButtonGroup, QSizePolicy,
    QScrollArea, QWidget,
)


class CorrectionDialog(QDialog):
    """
    Edit the manual polarity override for one *or more* components.

    Parameters
    ----------
    refs       : str or list[str]   — single ref or list of refs
    comp_types : str or list[str]   — matching comp_type(s)
    current    : dict               — for single mode: the correction dict;
                                      for bulk mode: {ref: correction_dict}
    """

    def __init__(
        self,
        refs:       Union[str, List[str]],
        comp_types: Union[str, List[str]],
        current:    dict,
        parent=None,
    ):
        super().__init__(parent)

        # Normalise to lists
        if isinstance(refs, str):
            refs       = [refs]
            comp_types = [comp_types] if isinstance(comp_types, str) else comp_types
            # current is a flat correction dict
            current_map: Dict[str, dict] = {refs[0]: current}
        else:
            if isinstance(comp_types, str):
                comp_types = [comp_types] * len(refs)
            # current may be a flat dict (legacy single call) or {ref: dict}
            if refs and isinstance(current.get(refs[0]), dict):
                current_map = current
            else:
                # Legacy: single flat dict passed for the first ref
                current_map = {r: current for r in refs}

        self._refs = refs
        bulk       = len(refs) > 1

        # ── Window title ──────────────────────────────────────────────────
        if bulk:
            self.setWindowTitle(f"Polarity correction — {len(refs)} components")
        else:
            self.setWindowTitle(f"Polarity correction — {refs[0]}")
        self.setMinimumWidth(420)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # ── Header ────────────────────────────────────────────────────────
        if bulk:
            # Show the list of refs in a compact scrollable area
            unique_types = sorted(set(comp_types))
            hdr = QLabel(
                f"<b>{len(refs)} components selected</b>  "
                f"<i>({', '.join(unique_types)})</i>"
            )
            hdr.setAlignment(Qt.AlignCenter)
            layout.addWidget(hdr)

            refs_lbl = QLabel("  ".join(refs))
            refs_lbl.setWordWrap(True)
            refs_lbl.setStyleSheet(
                "background:#f0f0f0; border:1px solid #ccc; "
                "border-radius:3px; padding:4px; font-size:9px; color:#333;"
            )
            layout.addWidget(refs_lbl)

            note_lbl = QLabel(
                "⚠  The same correction will be applied to <b>all</b> selected components."
            )
            note_lbl.setWordWrap(True)
            note_lbl.setStyleSheet("color:#b05000; font-size:9px;")
            layout.addWidget(note_lbl)
        else:
            unique_types = list(dict.fromkeys(comp_types))
            hdr = QLabel(f"<b>{refs[0]}</b>  <i>({unique_types[0]})</i>")
            hdr.setAlignment(Qt.AlignCenter)
            layout.addWidget(hdr)

        # ── Determine pre-fill values ─────────────────────────────────────
        # For each field: use the common value if all refs agree; else None
        all_polars = [current_map.get(r, {}).get("polar", None) for r in refs]
        all_flips  = [current_map.get(r, {}).get("flip_pin", False) for r in refs]
        all_notes  = [current_map.get(r, {}).get("note", "")        for r in refs]

        common_polar = all_polars[0] if len(set(
            str(p) for p in all_polars)) == 1 else "mixed"
        common_flip  = all_flips[0]  if all(f == all_flips[0]  for f in all_flips)  else False
        common_note  = all_notes[0]  if all(n == all_notes[0]  for n in all_notes)  else ""

        # ── Polar-status group ────────────────────────────────────────────
        grp        = QGroupBox("Polar status")
        grp_layout = QVBoxLayout(grp)

        self._rb_auto  = QRadioButton("Auto  (use ODB++ / heuristic detection)")
        self._rb_polar = QRadioButton("Force  polar  — always show marker")
        self._rb_none  = QRadioButton("Force  non-polar  — never show marker")
        self._rb_group = QButtonGroup(self)
        for rb in (self._rb_auto, self._rb_polar, self._rb_none):
            self._rb_group.addButton(rb)
            grp_layout.addWidget(rb)

        if bulk and common_polar == "mixed":
            self._rb_auto.setChecked(True)  # default to Auto when mixed
            mix_lbl = QLabel("  (mixed values — select to override all)")
            mix_lbl.setStyleSheet("color:#888; font-size:8px;")
            grp_layout.addWidget(mix_lbl)
        elif common_polar is True:
            self._rb_polar.setChecked(True)
        elif common_polar is False:
            self._rb_none.setChecked(True)
        else:
            self._rb_auto.setChecked(True)

        layout.addWidget(grp)

        # ── Flip pin checkbox ─────────────────────────────────────────────
        self._flip_cb = QCheckBox(
            "Flip pin  — mark the other pin instead of the auto-detected one\n"
            "(e.g. pin 1 instead of pin 2, or vice versa)"
        )
        self._flip_cb.setChecked(bool(common_flip))
        layout.addWidget(self._flip_cb)

        # ── Note field (single mode only) ─────────────────────────────────
        if not bulk:
            note_layout = QFormLayout()
            self._note_edit = QLineEdit(common_note)
            self._note_edit.setPlaceholderText("Optional comment …")
            note_layout.addRow("Note:", self._note_edit)
            layout.addLayout(note_layout)
        else:
            self._note_edit = None  # not used in bulk mode

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
        if self._flip_cb.isChecked():
            result["flip_pin"] = True
        if self._note_edit is not None:
            note = self._note_edit.text().strip()
            if note:
                result["note"] = note
        return result

