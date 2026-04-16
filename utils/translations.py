"""
utils/translations.py
─────────────────────
UI string translations for PolarityMark.
Supported languages: English (en), German (de).
"""

TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {
        # ── Language selector ──────────────────────────────────────────────
        "language_label": "Language:",

        # ── Window ────────────────────────────────────────────────────────
        "window_title": "PolarityMark – PCB Polarity Detector",

        # ── Input group ───────────────────────────────────────────────────
        "group_input": "Input files",
        "label_odb": "ODB++:",
        "placeholder_odb": "ODB++ source file (.tgz / .zip / directory) …",
        "btn_browse": "Browse…",
        "tooltip_odb_clear": "Clear ODB++ selection",
        "label_dnp": "DNP:",
        "tooltip_dnp_label": "Do-Not-Place components",
        "placeholder_dnp": "Do-Not-Place components (comma-separated, e.g.: R5, C3, D12, U4) …",
        "tooltip_dnp_edit": (
            "These components are highlighted orange in the PDF and preview.\n"
            "No polarity marker is drawn for them."
        ),
        "tooltip_dnp_clear": "Clear DNP list",

        # ── Options row ───────────────────────────────────────────────────
        "cb_debug": "Debug mode",
        "cb_save_pdf": "Save annotated PDF",
        "cb_fab": "Fab",
        "tooltip_fab": (
            "Fab/assembly layer — component body outlines.\n"
            "Many lines (~7000+), slower render."
        ),
        "cb_silk": "Silkscreen",
        "tooltip_silk": "Silkscreen layer — labels and polarity symbols.",
        "cb_court": "Courtyard",
        "tooltip_court": (
            "Courtyard layer — component boundary outlines.\n"
            "Few lines, fast render."
        ),
        "cb_notes": "Notes/User Drawing",
        "tooltip_notes": (
            "Notes/User Drawing layer — often contains the title block and border."
        ),
        "cb_title": "Title block",
        "tooltip_title": (
            "Drawing frame & title block (border, revision table, company logo area).\n"
            "Expands the page to include the full drawing frame.\n"
            "ODB++ layers: title_text_top/bottom, head_top/bottom."
        ),
        "cb_refdes": "RefDes",
        "tooltip_refdes": (
            "Show reference designators (e.g. U1, C3, D5) on the PDF.\n"
            "Disable for a cleaner view without component labels."
        ),
        "btn_analyze": "🔍  Analyze",
        "btn_rerender": "🔄  Re-render",
        "tooltip_rerender": "Re-render the ODB++ PDF with current manual corrections applied",

        # ── Log / Results ─────────────────────────────────────────────────
        "group_log": "Analysis Log",
        "group_results": "Results",
        "placeholder_search": "Search: ref / type / status / marker …",
        "col_ref": "Ref",
        "col_type": "Type",
        "col_page": "Page",
        "col_status": "Status",
        "col_conf": "Confidence",
        "col_markers": "Marker types",
        "btn_export_json": "💾  Export JSON",
        "btn_open_preview": "🔍  Open PDF Preview…",
        "tooltip_open_preview": (
            "Open the rendered PDF in a separate window.\n"
            "The window is resizable and supports fullscreen (F11)."
        ),

        # ── Status bar ────────────────────────────────────────────────────
        "status_ready": "Ready.  Load an ODB++ file to begin.",
        "status_analyzing": "Analyzing …",
        "status_odb_cleared": "ODB++ cleared.",
        "status_done": "Done — {n_results} components, {n_marked} with polarity markers  ({time_str})",
        "status_odb_loaded": "ODB++: {name}",
        "status_dnp_set": "DNP: {n} component(s) marked  —  Re-render to bake into PDF.",
        "status_corrections_loaded": "Loaded {n} manual correction(s) from sidecar.",
        "status_json_restored": "Restored {n} components from JSON — click '🔄 Re-render' to rebuild the PDF.",
        "status_json_restored_preview": "Restored {n} components from JSON  ({n_marked} marked)  —  PDF preview loaded.",

        # ── Dialogs ───────────────────────────────────────────────────────
        "dlg_open_rendered_title": "Open rendered PDF?",
        "dlg_open_rendered_msg": "Polarity PDF rendered from ODB++:\n{pdf_path}\n\nOpen now?",
        "dlg_open_annotated_title": "Open annotated PDF?",
        "dlg_open_annotated_msg": "Saved:\n{out_pdf}\n\nOpen now?",
        "dlg_open_rerendered_title": "Open updated PDF?",
        "dlg_open_rerendered_msg": "Re-rendered:\n{out_pdf}\n\nOpen now?",
        "dlg_rerender_no_odb_title": "Re-render",
        "dlg_rerender_no_odb_msg": "No ODB++ render to update yet.\nRun Analyze first.",
        "dlg_export_title": "Export",
        "dlg_export_msg": "JSON saved:\n{path}",
        "dlg_export_error_title": "Export Error",
        "dlg_save_json_title": "Save JSON",

        # ── Context menu ──────────────────────────────────────────────────
        "ctx_accept_pin": "✓  Accept current pin for {label}",
        "ctx_flip_accept": "↔  Flip & Accept for {label}",
        "ctx_edit_single": "✏️  Edit polarity correction for {ref}",
        "ctx_clear_single": "✕  Clear correction for {ref}",
        "ctx_edit_multi": "✏️  Edit correction for {n} components …",
        "ctx_clear_multi": "✕  Clear corrections for all {n}",
        "ctx_edit_preview_single": "✏️  Edit correction for {ref}",

        # ── Log messages ──────────────────────────────────────────────────
        "log_rerender_start": "\n🔄 Re-rendering with corrections …",
        "log_pdf_updated": "   PDF updated: {out_pdf}",
        "log_rerender_failed": "❌ Re-render failed: {exc}",
        "log_correction_saved": "✎ Correction {action} for {label}. Click '🔄 Re-render' to update the PDF.",
        "log_correction_saved_action": "saved",
        "log_correction_cleared_action": "cleared",
        "log_accepted": "✓ Accepted{flip_note}: {label}.  Click '🔄 Re-render' to update the PDF.",
        "log_accepted_flip_note": " (pin flipped)",
        "log_analysis_failed": "Analysis failed.",
    },

    "de": {
        # ── Language selector ──────────────────────────────────────────────
        "language_label": "Sprache:",

        # ── Window ────────────────────────────────────────────────────────
        "window_title": "PolarityMark – PCB-Polaritätsdetektor",

        # ── Input group ───────────────────────────────────────────────────
        "group_input": "Eingabedateien",
        "label_odb": "ODB++:",
        "placeholder_odb": "ODB++-Quelldatei (.tgz / .zip / Verzeichnis) …",
        "btn_browse": "Durchsuchen…",
        "tooltip_odb_clear": "ODB++-Auswahl löschen",
        "label_dnp": "DNP:",
        "tooltip_dnp_label": "Nicht zu bestückende Bauteile",
        "placeholder_dnp": "Nicht bestückte Bauteile (kommagetrennt, z.B.: R5, C3, D12, U4) …",
        "tooltip_dnp_edit": (
            "Diese Bauteile werden im PDF und der Vorschau orange hervorgehoben.\n"
            "Für sie wird kein Polaritätsmarker gezeichnet."
        ),
        "tooltip_dnp_clear": "DNP-Liste löschen",

        # ── Options row ───────────────────────────────────────────────────
        "cb_debug": "Debug-Modus",
        "cb_save_pdf": "Annotiertes PDF speichern",
        "cb_fab": "Fab",
        "tooltip_fab": (
            "Fab-/Bestückungslage — Bauteilkonturen.\n"
            "Viele Linien (~7000+), langsameres Rendern."
        ),
        "cb_silk": "Bestückungsdruck",
        "tooltip_silk": "Bestückungsdrucklage — Beschriftungen und Polaritätssymbole.",
        "cb_court": "Courtyard",
        "tooltip_court": (
            "Courtyard-Lage — Bauteilbegrenzungsrahmen.\n"
            "Wenige Linien, schnelles Rendern."
        ),
        "cb_notes": "Notizen/Zeichnung",
        "tooltip_notes": (
            "Notizen-/Benutzerzeichnungslage — enthält oft den Schriftfeld-Rahmen."
        ),
        "cb_title": "Schriftfeld",
        "tooltip_title": (
            "Zeichnungsrahmen & Schriftfeld (Rand, Revisonstabelle, Firmenlogofläche).\n"
            "Erweitert die Seite um den vollständigen Zeichnungsrahmen.\n"
            "ODB++-Lagen: title_text_top/bottom, head_top/bottom."
        ),
        "cb_refdes": "RefDes",
        "tooltip_refdes": (
            "Referenzbezeichnungen (z.B. U1, C3, D5) im PDF anzeigen.\n"
            "Deaktivieren für eine übersichtlichere Ansicht ohne Bauteilbeschriftungen."
        ),
        "btn_analyze": "🔍  Analysieren",
        "btn_rerender": "🔄  Neu rendern",
        "tooltip_rerender": "ODB++-PDF mit aktuellen manuellen Korrekturen neu rendern",

        # ── Log / Results ─────────────────────────────────────────────────
        "group_log": "Analyseprotokoll",
        "group_results": "Ergebnisse",
        "placeholder_search": "Suche: Ref / Typ / Status / Marker …",
        "col_ref": "Ref",
        "col_type": "Typ",
        "col_page": "Seite",
        "col_status": "Status",
        "col_conf": "Konfidenz",
        "col_markers": "Markertypen",
        "btn_export_json": "💾  JSON exportieren",
        "btn_open_preview": "🔍  PDF-Vorschau öffnen…",
        "tooltip_open_preview": (
            "Das gerenderte PDF in einem separaten Fenster öffnen.\n"
            "Das Fenster ist in der Größe veränderbar und unterstützt Vollbild (F11)."
        ),

        # ── Status bar ────────────────────────────────────────────────────
        "status_ready": "Bereit.  ODB++-Datei laden, um zu beginnen.",
        "status_analyzing": "Analyse läuft …",
        "status_odb_cleared": "ODB++ gelöscht.",
        "status_done": "Fertig — {n_results} Bauteile, {n_marked} mit Polaritätsmarkern  ({time_str})",
        "status_odb_loaded": "ODB++: {name}",
        "status_dnp_set": "DNP: {n} Bauteil(e) markiert  —  Neu rendern zum Übernehmen.",
        "status_corrections_loaded": "{n} manuelle Korrektur(en) aus Sidecar geladen.",
        "status_json_restored": "Ergebnisse wiederhergestellt ({n} Bauteile) — '🔄 Neu rendern' zum Neuerstellen des PDFs.",
        "status_json_restored_preview": "{n} Bauteile aus JSON geladen  ({n_marked} markiert)  —  PDF-Vorschau geladen.",

        # ── Dialogs ───────────────────────────────────────────────────────
        "dlg_open_rendered_title": "Gerendertes PDF öffnen?",
        "dlg_open_rendered_msg": "Polaritäts-PDF aus ODB++ gerendert:\n{pdf_path}\n\nJetzt öffnen?",
        "dlg_open_annotated_title": "Annotiertes PDF öffnen?",
        "dlg_open_annotated_msg": "Gespeichert:\n{out_pdf}\n\nJetzt öffnen?",
        "dlg_open_rerendered_title": "Aktualisiertes PDF öffnen?",
        "dlg_open_rerendered_msg": "Neu gerendert:\n{out_pdf}\n\nJetzt öffnen?",
        "dlg_rerender_no_odb_title": "Neu rendern",
        "dlg_rerender_no_odb_msg": "Noch kein ODB++-Render zum Aktualisieren.\nZuerst Analysieren ausführen.",
        "dlg_export_title": "Exportieren",
        "dlg_export_msg": "JSON gespeichert:\n{path}",
        "dlg_export_error_title": "Exportfehler",
        "dlg_save_json_title": "JSON speichern",

        # ── Context menu ──────────────────────────────────────────────────
        "ctx_accept_pin": "✓  Aktuellen Pin akzeptieren für {label}",
        "ctx_flip_accept": "↔  Umkehren & Akzeptieren für {label}",
        "ctx_edit_single": "✏️  Polaritätskorrektur bearbeiten für {ref}",
        "ctx_clear_single": "✕  Korrektur löschen für {ref}",
        "ctx_edit_multi": "✏️  Korrektur bearbeiten für {n} Bauteile …",
        "ctx_clear_multi": "✕  Korrekturen für alle {n} löschen",
        "ctx_edit_preview_single": "✏️  Korrektur bearbeiten für {ref}",

        # ── Log messages ──────────────────────────────────────────────────
        "log_rerender_start": "\n🔄 Neu rendern mit Korrekturen …",
        "log_pdf_updated": "   PDF aktualisiert: {out_pdf}",
        "log_rerender_failed": "❌ Neu rendern fehlgeschlagen: {exc}",
        "log_correction_saved": "✎ Korrektur {action} für {label}. '🔄 Neu rendern' zum Aktualisieren des PDFs.",
        "log_correction_saved_action": "gespeichert",
        "log_correction_cleared_action": "gelöscht",
        "log_accepted": "✓ Akzeptiert{flip_note}: {label}.  '🔄 Neu rendern' zum Aktualisieren des PDFs.",
        "log_accepted_flip_note": " (Pin umgekehrt)",
        "log_analysis_failed": "Analyse fehlgeschlagen.",
    },

    "hu": {
        # ── Language selector ──────────────────────────────────────────────
        "language_label": "Nyelv:",

        # ── Window ────────────────────────────────────────────────────────
        "window_title": "PolarityMark – PCB Polaritásdetektor",

        # ── Input group ───────────────────────────────────────────────────
        "group_input": "Bemeneti fájlok",
        "label_odb": "ODB++:",
        "placeholder_odb": "ODB++ forrásfájl (.tgz / .zip / könyvtár) …",
        "btn_browse": "Tallózás…",
        "tooltip_odb_clear": "ODB++ kijelölés törlése",
        "label_dnp": "DNP:",
        "tooltip_dnp_label": "Nem beültetendő alkatrészek",
        "placeholder_dnp": "Nem beültetendő alkatrészek (vesszővel elválasztva, pl.: R5, C3, D12, U4) …",
        "tooltip_dnp_edit": (
            "Ezek az alkatrészek narancssárgával kiemelve jelennek meg a PDF-ben és az előnézetben.\n"
            "Számukra nem kerül polaritásjelölő rajzolásra."
        ),
        "tooltip_dnp_clear": "DNP-lista törlése",

        # ── Options row ───────────────────────────────────────────────────
        "cb_debug": "Hibakeresési mód",
        "cb_save_pdf": "Annotált PDF mentése",
        "cb_fab": "Fab",
        "tooltip_fab": (
            "Fab/szerelési réteg — alkatrész-körvonalak.\n"
            "Sok vonal (~7000+), lassabb renderelés."
        ),
        "cb_silk": "Szitanyomat",
        "tooltip_silk": "Szitanyomat-réteg — feliratok és polaritásszimbólumok.",
        "cb_court": "Courtyard",
        "tooltip_court": (
            "Courtyard-réteg — alkatrészhatárokat jelző körvonalak.\n"
            "Kevés vonal, gyors renderelés."
        ),
        "cb_notes": "Megjegyzések/Rajz",
        "tooltip_notes": (
            "Megjegyzések/Felhasználói rajz réteg — gyakran tartalmazza a rajzkeretet és szegélyt."
        ),
        "cb_title": "Fejléc",
        "tooltip_title": (
            "Rajzkeret és fejléc (szegély, revíziós táblázat, céglogó terület).\n"
            "Kiterjeszti az oldalt a teljes rajzkeret befoglalásához.\n"
            "ODB++ rétegek: title_text_top/bottom, head_top/bottom."
        ),
        "cb_refdes": "RefDes",
        "tooltip_refdes": (
            "Referenciaszámok megjelenítése (pl. U1, C3, D5) a PDF-en.\n"
            "Kikapcsolásával tisztább nézet érhető el alkatrész-feliratok nélkül."
        ),
        "btn_analyze": "🔍  Elemzés",
        "btn_rerender": "🔄  Újrarenderelés",
        "tooltip_rerender": "Az ODB++ PDF újrarenderelése a jelenlegi manuális javításokkal",

        # ── Log / Results ─────────────────────────────────────────────────
        "group_log": "Elemzési napló",
        "group_results": "Eredmények",
        "placeholder_search": "Keresés: ref / típus / státusz / marker …",
        "col_ref": "Ref",
        "col_type": "Típus",
        "col_page": "Oldal",
        "col_status": "Státusz",
        "col_conf": "Megbízhatóság",
        "col_markers": "Markertípusok",
        "btn_export_json": "💾  JSON exportálása",
        "btn_open_preview": "🔍  PDF-előnézet megnyitása…",
        "tooltip_open_preview": (
            "A renderelt PDF megnyitása egy különálló ablakban.\n"
            "Az ablak átméretezhető, és támogatja a teljes képernyős nézetet (F11)."
        ),

        # ── Status bar ────────────────────────────────────────────────────
        "status_ready": "Kész.  Töltsön be egy ODB++ fájlt a kezdéshez.",
        "status_analyzing": "Elemzés folyamatban …",
        "status_odb_cleared": "ODB++ törölve.",
        "status_done": "Kész — {n_results} alkatrész, {n_marked} polaritásjelölővel  ({time_str})",
        "status_odb_loaded": "ODB++: {name}",
        "status_dnp_set": "DNP: {n} alkatrész megjelölve  —  Újrarenderelés szükséges a PDF-be való beégetéshez.",
        "status_corrections_loaded": "{n} manuális javítás betöltve a sidecar fájlból.",
        "status_json_restored": "Eredmények visszaállítva ({n} alkatrész) — kattintson a '🔄 Újrarenderelés' gombra a PDF újraépítéséhez.",
        "status_json_restored_preview": "{n} alkatrész visszaállítva JSON-ból  ({n_marked} megjelölve)  —  PDF-előnézet betöltve.",

        # ── Dialogs ───────────────────────────────────────────────────────
        "dlg_open_rendered_title": "Megnyitja a renderelt PDF-et?",
        "dlg_open_rendered_msg": "Polaritás PDF renderelve ODB++-ból:\n{pdf_path}\n\nMegnyitja most?",
        "dlg_open_annotated_title": "Megnyitja az annotált PDF-et?",
        "dlg_open_annotated_msg": "Mentve:\n{out_pdf}\n\nMegnyitja most?",
        "dlg_open_rerendered_title": "Megnyitja a frissített PDF-et?",
        "dlg_open_rerendered_msg": "Újrarenderelve:\n{out_pdf}\n\nMegnyitja most?",
        "dlg_rerender_no_odb_title": "Újrarenderelés",
        "dlg_rerender_no_odb_msg": "Még nincs ODB++ render a frissítéshez.\nElőször futtassa az Elemzést.",
        "dlg_export_title": "Exportálás",
        "dlg_export_msg": "JSON mentve:\n{path}",
        "dlg_export_error_title": "Exportálási hiba",
        "dlg_save_json_title": "JSON mentése",

        # ── Context menu ──────────────────────────────────────────────────
        "ctx_accept_pin": "✓  Jelenlegi tüske elfogadása: {label}",
        "ctx_flip_accept": "↔  Megfordítás és elfogadás: {label}",
        "ctx_edit_single": "✏️  Polaritásjavítás szerkesztése: {ref}",
        "ctx_clear_single": "✕  Javítás törlése: {ref}",
        "ctx_edit_multi": "✏️  Javítás szerkesztése {n} alkatrészhez …",
        "ctx_clear_multi": "✕  Összes javítás törlése ({n})",
        "ctx_edit_preview_single": "✏️  Javítás szerkesztése: {ref}",

        # ── Log messages ──────────────────────────────────────────────────
        "log_rerender_start": "\n🔄 Újrarenderelés javításokkal …",
        "log_pdf_updated": "   PDF frissítve: {out_pdf}",
        "log_rerender_failed": "❌ Újrarenderelés sikertelen: {exc}",
        "log_correction_saved": "✎ Javítás {action}: {label}. Kattintson a '🔄 Újrarenderelés' gombra a PDF frissítéséhez.",
        "log_correction_saved_action": "mentve",
        "log_correction_cleared_action": "törölve",
        "log_accepted": "✓ Elfogadva{flip_note}: {label}.  Kattintson a '🔄 Újrarenderelés' gombra a PDF frissítéséhez.",
        "log_accepted_flip_note": " (tüske megfordítva)",
        "log_analysis_failed": "Az elemzés sikertelen.",
    },
}

LANGUAGE_NAMES = {
    "en": "English",
    "de": "Deutsch",
    "hu": "Magyar",
}



