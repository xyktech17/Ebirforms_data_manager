import sys
import os
import re
import json
import zipfile
import tempfile
from datetime import datetime

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QFileDialog, QTextEdit, QComboBox,
    QMessageBox, QTableWidget, QTableWidgetItem, QHeaderView, QDialog,
    QGroupBox, QAbstractItemView
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

# ==============================================================================
# INDESTRUCTIBLE REGEX ENGINE (Bypasses Broken BIR XMLs)
# ==============================================================================

def parse_xml_record(file_path):
    """Extracts Taxpayer Name, TIN, and modified timestamp using raw Regex."""
    mtime = os.path.getmtime(file_path)
    mtime_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
    filename = os.path.basename(file_path)

    try:
        # Read raw text, ignoring any BIR encoding corruption
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            xml_data = f.read()
        
        tin_parts = {}
        tin_full = None
        name = None

        # 1. Hunt for TIN and Branch Code tags
        for m in re.finditer(r'<([a-zA-Z0-9_:-]+)[^>]*>([^<]+)</\1>', xml_data):
            tag = m.group(1).lower()
            val = m.group(2).strip()
            if not val: continue

            if 'tin1' in tag: tin_parts['1'] = val
            elif 'tin2' in tag: tin_parts['2'] = val
            elif 'tin3' in tag: tin_parts['3'] = val
            elif tag in ['tin4', 'branchcode', 'txtbranchcode', 'branch']: tin_parts['branch'] = val
            elif 'tin' in tag and (re.match(r'^\d{9,14}$', val) or re.match(r'^\d{3}-\d{3}-\d{3}', val)):
                if not tin_full: tin_full = val

            if not name:
                if any(k in tag for k in ['taxpayername', 'txttaxpayername', 'registeredname', 'txtregisteredname', 'corporatename']):
                    name = val

        # 2. Hunt for Individual Names if Corporate isn't found
        if not name:
            first_m = re.search(r'<[a-zA-Z0-9_:-]*firstname[^>]*>([^<]+)</', xml_data, re.IGNORECASE)
            last_m = re.search(r'<[a-zA-Z0-9_:-]*lastname[^>]*>([^<]+)</', xml_data, re.IGNORECASE)
            first = first_m.group(1).strip() if first_m else ""
            last = last_m.group(1).strip() if last_m else ""
            if first or last:
                name = f"{last}, {first}".strip(", ")
        
        # 3. Assemble the Canonical TIN
        if '1' in tin_parts and '2' in tin_parts and '3' in tin_parts:
            b = tin_parts.get('branch', '000')
            canonical_tin = f"{tin_parts['1']}-{tin_parts['2']}-{tin_parts['3']}-{b}"
        elif tin_full:
            canonical_tin = tin_full
        else:
            # Ultimate Fallback: Scrape the Filename
            match = re.search(r'(\d{9,14})', filename)
            if match:
                canonical_tin = match.group(1)
            else:
                canonical_tin = "Unknown-TIN"
        
        clean_tin = re.sub(r'[^0-9]', '', canonical_tin)[:9]

        return {
            'path': file_path,
            'filename': filename,
            'tin': canonical_tin,
            'base_tin': clean_tin,
            'name': name or "Unknown Taxpayer",
            'mtime': mtime,
            'mtime_str': mtime_str
        }
    except Exception:
        return None

def transform_filename(filename, mode, custom_suffix=""):
    """Updates the TIN found inside the filename without touching dates or forms."""
    name, ext = os.path.splitext(filename)
    if mode == "append_00":
        name = re.sub(r'(?<!\d)(\d{12})(?!\d)', r'\g<1>00', name)
    elif mode == "remove_00":
        name = re.sub(r'(?<!\d)(\d{12})00(?!\d)', r'\g<1>', name)
    elif mode == "custom":
        name = re.sub(r'(?<!\d)(\d{12})(?!\d)', r'\g<1>' + custom_suffix, name)
    return name + ext

def transform_xml_file(source_path, dest_path, mode, custom_suffix=""):
    """Raw text replacement. Immune to XML formatting crashes."""
    with open(source_path, 'r', encoding='utf-8', errors='ignore') as f:
        xml_data = f.read()

    if mode != "none":
        def replacer(match):
            open_tag = match.group(1)
            tag_name = match.group(2).lower()
            text = match.group(3).strip()
            close_tag = match.group(4)

            is_branch_tag = any(k in tag_name for k in ['branchcode', 'branch_code', 'tin4', 'branch', 'txtbranchcode'])
            new_text = text
            
            if mode == "append_00":
                if is_branch_tag and len(text) == 3 and text.isdigit(): new_text = text + "00"
                elif re.match(r'^\d{12}$', text): new_text = text + "00"
                elif re.match(r'^\d{3}-\d{3}-\d{3}-\d{3}$', text): new_text = text + "00"
            elif mode == "remove_00":
                if is_branch_tag and len(text) == 5 and text.endswith("00"): new_text = text[:-2]
                elif re.match(r'^\d{14}$', text) and text.endswith("00"): new_text = text[:-2]
                elif re.match(r'^\d{3}-\d{3}-\d{3}-\d{5}$', text) and text.endswith("00"): new_text = text[:-2]
            elif mode == "custom":
                if is_branch_tag and len(text) == 3 and text.isdigit(): new_text = text + custom_suffix
                elif re.match(r'^\d{12}$', text): new_text = text + custom_suffix
                elif re.match(r'^\d{3}-\d{3}-\d{3}-\d{3}$', text): new_text = text + custom_suffix
            
            return f"{open_tag}{new_text}{close_tag}"

        # Match tags and contents and pass to replacer
        xml_data = re.sub(r'(<([a-zA-Z0-9_:-]+)[^>]*>)([^<]+)(</\2>)', replacer, xml_data)

    # Write output
    with open(dest_path, 'w', encoding='utf-8') as f:
        f.write(xml_data)

# ==============================================================================
# CONFLICT RESOLUTION MODAL DIALOG
# ==============================================================================

class ConflictDialog(QDialog):
    RES_OVERWRITE = 1
    RES_SKIP = 2
    RES_OVERWRITE_ALL = 3
    RES_SKIP_ALL = 4

    def __init__(self, taxpayer_name, tin, src_date, dest_date, is_dest_newer, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Conflict Detected")
        self.setFixedSize(500, 260)
        self.user_choice = self.RES_SKIP

        layout = QVBoxLayout(self)
        info_label = QLabel(
            f"<b>Taxpayer:</b> {taxpayer_name}<br>"
            f"<b>TIN:</b> {tin}<br><br>"
            f"<b>Source Record Last Update:</b> {src_date}<br>"
            f"<b>Destination (Existing) Last Update:</b> {dest_date}"
        )
        info_label.setTextFormat(Qt.RichText)
        layout.addWidget(info_label)

        if is_dest_newer:
            warn = QLabel("⚠️ Notice: The EXISTING record in the destination is NEWER.")
            warn.setStyleSheet("color: #D32F2F; font-weight: bold;")
        else:
            warn = QLabel("ℹ️ Notice: The SOURCE record is NEWER than the existing destination record.")
            warn.setStyleSheet("color: #1976D2; font-weight: bold;")
        layout.addWidget(warn)
        layout.addWidget(QLabel("Choose an action:"))

        btn_layout = QHBoxLayout()
        for text, choice in [("Overwrite", self.RES_OVERWRITE), ("Skip", self.RES_SKIP), 
                             ("Overwrite All", self.RES_OVERWRITE_ALL), ("Skip All", self.RES_SKIP_ALL)]:
            btn = QPushButton(text)
            btn.clicked.connect(lambda checked, c=choice: self.set_choice(c))
            btn_layout.addWidget(btn)
        layout.addLayout(btn_layout)

    def set_choice(self, choice):
        self.user_choice = choice
        self.accept()

# ==============================================================================
# MAIN APPLICATION WINDOW
# ==============================================================================

class EBIRManagerApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("eBIRForms Data Migrator & Transfer Manager")
        self.resize(1050, 720)
        self.source_records = {}       
        self.dest_records = {}         
        self.init_ui()

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        folder_group = QGroupBox("Directory Setup")
        folder_layout = QVBoxLayout(folder_group)
        for label_text, attr_name in [("Source / Old eBIR Main Folder (e.g. Offline eBIRForms Package v7.x):", "src_input"), 
                                      ("Destination / New eBIR Main Folder (e.g. Offline eBIRForms Package v8.x):", "dest_input")]:
            row = QHBoxLayout()
            row.addWidget(QLabel(label_text))
            input_field = QLineEdit()
            setattr(self, attr_name, input_field)
            btn = QPushButton("Browse...")
            btn.clicked.connect(self.browse_source if attr_name == "src_input" else self.browse_dest)
            row.addWidget(input_field)
            row.addWidget(btn)
            folder_layout.addLayout(row)
        main_layout.addWidget(folder_group)

        ctrl_bar = QHBoxLayout()
        self.scan_btn = QPushButton("🔄 Scan & Compare Records")
        self.scan_btn.setStyleSheet("font-weight: bold; padding: 6px 12px;")
        self.scan_btn.clicked.connect(self.scan_records)
        ctrl_bar.addWidget(self.scan_btn)

        ctrl_bar.addSpacing(20)
        ctrl_bar.addWidget(QLabel("Search/Filter:"))
        self.search_input = QLineEdit()
        self.search_input.textChanged.connect(self.filter_table)
        ctrl_bar.addWidget(self.search_input)

        for text, state in [("Select All", True), ("Deselect All", False)]:
            btn = QPushButton(text)
            btn.clicked.connect(lambda checked, s=state: self.set_all_checkboxes(s))
            ctrl_bar.addWidget(btn)
        main_layout.addLayout(ctrl_bar)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Select", "Taxpayer / Company Name", "TIN", "Forms Count", "Source Last Updated", "Destination Status"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        main_layout.addWidget(self.table)

        tin_box = QGroupBox("TIN Format Options (Updates BOTH Filenames and XML tags)")
        tin_layout = QHBoxLayout(tin_box)
        self.tin_combo = QComboBox()
        self.tin_combo.addItem("Convert 3-digit to 5-digit Branch Code (Append '00')", "append_00")
        self.tin_combo.addItem("Keep Original / No Transformation", "none")
        self.tin_combo.addItem("Revert 5-digit to 3-digit Branch Code (Remove '00')", "remove_00")
        self.tin_combo.addItem("Custom Suffix...", "custom")
        self.tin_combo.currentIndexChanged.connect(self.toggle_custom_suffix)

        self.custom_suffix_input = QLineEdit()
        self.custom_suffix_input.setPlaceholderText("Suffix (e.g. 000)")
        self.custom_suffix_input.setEnabled(False)
        tin_layout.addWidget(QLabel("TIN Transform:"))
        tin_layout.addWidget(self.tin_combo)
        tin_layout.addWidget(self.custom_suffix_input)
        main_layout.addWidget(tin_box)

        action_bar = QHBoxLayout()
        buttons = [
            ("🚀 Transfer Selected to New eBIR", "#2E7D32", self.transfer_selected),
            ("💾 Export Backup Package (.ebirbak)", "#1565C0", self.export_backup),
            ("📂 Import Backup from Another PC", "#6A1B9A", self.import_backup)
        ]
        for text, color, func in buttons:
            btn = QPushButton(text)
            btn.setStyleSheet(f"background-color: {color}; color: white; font-weight: bold; padding: 10px;")
            btn.clicked.connect(func)
            action_bar.addWidget(btn)
        main_layout.addLayout(action_bar)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(120)
        main_layout.addWidget(self.log_view)

    def browse_source(self):
        if folder := QFileDialog.getExistingDirectory(self, "Select Old eBIRForms Main Folder"):
            self.src_input.setText(folder)

    def browse_dest(self):
        if folder := QFileDialog.getExistingDirectory(self, "Select Destination eBIRForms Main Folder"):
            self.dest_input.setText(folder)

    def toggle_custom_suffix(self):
        self.custom_suffix_input.setEnabled(self.tin_combo.currentData() == "custom")

    def log(self, text):
        self.log_view.append(f"[{datetime.now().strftime('%H:%M:%S')}] {text}")
        self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())
        QApplication.processEvents() # Keeps UI responsive during heavy logging

    def scan_records(self):
        src, dest = self.src_input.text().strip(), self.dest_input.text().strip()
        if not os.path.isdir(src):
            return QMessageBox.warning(self, "Error", "Select a valid Source folder.")

        self.log_view.clear()
        self.source_records = self._read_records(src)
        self.dest_records = self._read_records(dest) if os.path.isdir(dest) else {}
        self.populate_table()
        self.log(f"Scan complete. Found {len(self.source_records)} unique profiles.")

    def _read_records(self, folder):
        grouped = {}
        for root_dir, _, files in os.walk(folder):
            for file in files:
                if file.lower().endswith(".xml"):
                    full_path = os.path.join(root_dir, file)
                    rel_dir = os.path.relpath(root_dir, folder)
                    if rel_dir == '.':
                        rel_dir = "" 
                    
                    try:
                        rec = parse_xml_record(full_path)
                        if rec and rec['base_tin']:
                            rec['rel_dir'] = rel_dir
                            b_tin = rec['base_tin']
                            if b_tin not in grouped:
                                grouped[b_tin] = {'name': rec['name'], 'tin': rec['tin'], 'base_tin': b_tin, 'files': [], 'latest_mtime': rec['mtime'], 'latest_str': rec['mtime_str']}
                            grouped[b_tin]['files'].append(rec)
                            
                            if rec['mtime'] > grouped[b_tin]['latest_mtime']:
                                grouped[b_tin].update({'latest_mtime': rec['mtime'], 'latest_str': rec['mtime_str']})
                                if rec['name'] != "Unknown Taxpayer" and "Recovered" not in rec['name']: 
                                    grouped[b_tin]['name'] = rec['name']
                    except Exception as e:
                        self.log(f"⚠️ Skipped {file}: {str(e)}")
        return grouped

    def populate_table(self):
        self.table.setRowCount(0)
        self.table.setSortingEnabled(False)
        for base_tin, rec in self.source_records.items():
            r = self.table.rowCount()
            self.table.insertRow(r)
            
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(Qt.Checked)
            chk.setData(Qt.UserRole, base_tin)
            self.table.setItem(r, 0, chk)
            self.table.setItem(r, 1, QTableWidgetItem(rec['name']))
            self.table.setItem(r, 2, QTableWidgetItem(rec['tin']))
            self.table.setItem(r, 3, QTableWidgetItem(str(len(rec['files']))))
            self.table.setItem(r, 4, QTableWidgetItem(rec['latest_str']))

            dest_item = QTableWidgetItem()
            if base_tin in self.dest_records:
                d_rec = self.dest_records[base_tin]
                if d_rec['latest_mtime'] > rec['latest_mtime']:
                    dest_item.setText("Exists (Dest is NEWER)"); dest_item.setForeground(QColor("#D32F2F"))
                elif d_rec['latest_mtime'] < rec['latest_mtime']:
                    dest_item.setText("Exists (Source is NEWER)"); dest_item.setForeground(QColor("#1976D2"))
                else:
                    dest_item.setText("Exists (Same timestamp)"); dest_item.setForeground(QColor("#388E3C"))
            else:
                dest_item.setText("New Record"); dest_item.setForeground(QColor("#388E3C"))
            self.table.setItem(r, 5, dest_item)
        self.table.setSortingEnabled(True)

    def filter_table(self, q):
        q = q.lower()
        for r in range(self.table.rowCount()):
            self.table.setRowHidden(r, q not in self.table.item(r, 1).text().lower() and q not in self.table.item(r, 2).text().lower())

    def set_all_checkboxes(self, c):
        for r in range(self.table.rowCount()):
            if i := self.table.item(r, 0): i.setCheckState(Qt.Checked if c else Qt.Unchecked)

    def get_selected_tins(self):
        return [self.table.item(r, 0).data(Qt.UserRole) for r in range(self.table.rowCount()) if self.table.item(r, 0) and self.table.item(r, 0).checkState() == Qt.Checked]

    # --- TRANSFER LOGIC ---
    def transfer_selected(self):
        dest = self.dest_input.text().strip()
        tins = self.get_selected_tins()
        if not dest or not tins: return QMessageBox.warning(self, "Error", "Select destination and taxpayers.")
        os.makedirs(dest, exist_ok=True)

        mode, suffix = self.tin_combo.currentData(), self.custom_suffix_input.text().strip()
        ow_all, skip_all, t_cnt, s_cnt = False, False, 0, 0

        for b_tin in tins:
            s_rec = self.source_records[b_tin]
            if b_tin in self.dest_records:
                if not ow_all and not skip_all:
                    dlg = ConflictDialog(s_rec['name'], s_rec['tin'], s_rec['latest_str'], self.dest_records[b_tin]['latest_str'], self.dest_records[b_tin]['latest_mtime'] > s_rec['latest_mtime'], self)
                    dlg.exec()
                    if dlg.user_choice == ConflictDialog.RES_SKIP: s_cnt += 1; continue
                    elif dlg.user_choice == ConflictDialog.RES_SKIP_ALL: skip_all = True; s_cnt += 1; continue
                    elif dlg.user_choice == ConflictDialog.RES_OVERWRITE_ALL: ow_all = True
                elif skip_all: s_cnt += 1; continue

            for f in s_rec['files']:
                target_dir = os.path.join(dest, f['rel_dir'])
                os.makedirs(target_dir, exist_ok=True)

                new_filename = transform_filename(f['filename'], mode, suffix)
                dest_file_path = os.path.join(target_dir, new_filename)

                try:
                    transform_xml_file(f['path'], dest_file_path, mode, suffix)
                    t_cnt += 1
                except Exception as e:
                    self.log(f"Error transferring {f['filename']}: {e}")
            self.log(f"Transferred: {s_rec['name']}")
            
        QMessageBox.information(self, "Complete", f"Done!\nTransferred: {t_cnt} files\nSkipped: {s_cnt} taxpayers")
        self.scan_records()

    # --- EXPORT / IMPORT LOGIC ---
    def export_backup(self):
        tins = self.get_selected_tins()
        if not tins: return QMessageBox.information(self, "Error", "No records selected.")
        path, _ = QFileDialog.getSaveFileName(self, "Save Backup", "ebir_backup.ebirbak", "Backup (*.ebirbak *.zip)")
        if not path: return

        mode, suffix = self.tin_combo.currentData(), self.custom_suffix_input.text().strip()
        manifest = {'created_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 'records': []}

        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
            for b_tin in tins:
                rec = self.source_records[b_tin]
                entry = {'base_tin': b_tin, 'tin': rec['tin'], 'name': rec['name'], 'latest_mtime': rec['latest_mtime'], 'latest_str': rec['latest_str'], 'files': []}
                for f in rec['files']:
                    new_filename = transform_filename(f['filename'], mode, suffix)
                    arcname = f"data/{f['rel_dir']}/{new_filename}" if f['rel_dir'] else f"data/{new_filename}"
                    
                    temp_file = os.path.join(tempfile.gettempdir(), new_filename)
                    try:
                        transform_xml_file(f['path'], temp_file, mode, suffix)
                        z.write(temp_file, arcname=arcname)
                        os.remove(temp_file)
                        entry['files'].append({'filename': new_filename, 'rel_dir': f['rel_dir'], 'arcname': arcname, 'mtime': f['mtime']})
                    except Exception as e:
                        self.log(f"Error exporting {f['filename']}: {e}")
                manifest['records'].append(entry)
            z.writestr("manifest.json", json.dumps(manifest, indent=2))
            
        self.log(f"Exported to {path}")
        QMessageBox.information(self, "Success", "Backup exported successfully!")

    def import_backup(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Backup", "", "Backup (*.ebirbak *.zip)")
        if not path: return
        dest = self.dest_input.text().strip() or QFileDialog.getExistingDirectory(self, "Select Destination")
        if not dest: return
        self.dest_input.setText(dest)
        os.makedirs(dest, exist_ok=True)
        self.dest_records = self._read_records(dest)

        try:
            with zipfile.ZipFile(path, 'r') as z:
                manifest = json.loads(z.read('manifest.json').decode('utf-8'))
                ow_all, skip_all, i_cnt, s_cnt = False, False, 0, 0

                for rec in manifest.get('records', []):
                    b_tin = rec['base_tin']
                    if b_tin in self.dest_records:
                        if not ow_all and not skip_all:
                            dlg = ConflictDialog(rec['name'], rec['tin'], rec['latest_str'], self.dest_records[b_tin]['latest_str'], self.dest_records[b_tin]['latest_mtime'] > rec['latest_mtime'], self)
                            dlg.exec()
                            if dlg.user_choice == ConflictDialog.RES_SKIP: s_cnt += 1; continue
                            elif dlg.user_choice == ConflictDialog.RES_SKIP_ALL: skip_all = True; s_cnt += 1; continue
                            elif dlg.user_choice == ConflictDialog.RES_OVERWRITE_ALL: ow_all = True
                        elif skip_all: s_cnt += 1; continue

                    for f in rec['files']:
                        target_dir = os.path.join(dest, f.get('rel_dir', ''))
                        os.makedirs(target_dir, exist_ok=True)
                        dest_f = os.path.join(target_dir, f['filename'])
                        
                        with open(dest_f, 'wb') as out: out.write(z.read(f['arcname']))
                        mtime = f.get('mtime', datetime.now().timestamp())
                        os.utime(dest_f, (mtime, mtime))
                        i_cnt += 1
                
                QMessageBox.information(self, "Complete", f"Imported: {i_cnt} files\nSkipped: {s_cnt} taxpayers")
                self.scan_records()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to import: {e}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = EBIRManagerApp()
    window.show()
    sys.exit(app.exec())