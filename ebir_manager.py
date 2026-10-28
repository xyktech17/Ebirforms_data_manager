import sys
import os
import re
import json
import html
import shutil
import zipfile
import tempfile
import calendar
from datetime import datetime
from urllib.parse import unquote

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QFileDialog, QTextEdit, QComboBox,
    QMessageBox, QTableWidget, QTableWidgetItem, QHeaderView, QDialog,
    QGroupBox, QAbstractItemView, QGridLayout, QCheckBox
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

BACKUP_DIR_NAME = "_ebir_migrator_backups"

# ==============================================================================
# REGEX ENGINE & XML HELPERS
# ==============================================================================

def extract_ebir_key_values(xml_data):
    """
    Extracts (key, value) pairs from both eBIRForms' custom <div>key=valuekey=</div>
    format and standard <tag>value</tag> XML nodes.
    """
    pairs = []

    # 1. eBIRForms <div>key=valuekey=</div> format
    for m in re.finditer(r'<div[^>]*>\s*([a-zA-Z0-9_:-]+)=(.*?)\1=\s*</div>', xml_data, flags=re.IGNORECASE | re.DOTALL):
        pairs.append((m.group(1).strip(), m.group(2).strip()))

    # 2. Standard <tag>value</tag> format (ignoring outer <div> tags)
    for m in re.finditer(r'<([a-zA-Z0-9_:-]+)[^>]*>([^<]+)</[a-zA-Z0-9_:-]+>', xml_data, flags=re.IGNORECASE):
        tag = m.group(1).strip()
        if tag.lower() != 'div':
            pairs.append((tag, m.group(2).strip()))

    return pairs

def parse_xml_record(file_path):
    """Extracts Taxpayer Name, TIN, and modified timestamp from eBIRForms XML files."""
    mtime = os.path.getmtime(file_path)
    mtime_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
    filename = os.path.basename(file_path)

    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            xml_data = f.read()
        
        tin_parts = {}
        tin_full = None
        primary_name = None
        trade_name = None
        last_name = ""
        first_name = ""
        middle_name = ""

        excluded_subkeys = (
            'spouse', 'pres', 'treas', 'sign', 'rep', 'partner', 'donee',
            'beneficiary', 'employer', 'prev', 'acct', 'auditor', 'heirs',
            'father', 'mother', 'confirm'
        )

        for raw_key, raw_val in extract_ebir_key_values(xml_data):
            if not raw_val:
                continue
            # Decode both URL percent-encoding (%20, %2C) and HTML entities (&amp;)
            val = html.unescape(unquote(raw_val)).strip()
            if not val:
                continue

            short_key = raw_key.split(':')[-1].lower()

            # --- 1. Detect TIN & Branch Code ---
            if short_key in ('tin1', 'txttin1', 'tinpart1'):
                tin_parts['1'] = val
            elif short_key in ('tin2', 'txttin2', 'tinpart2'):
                tin_parts['2'] = val
            elif short_key in ('tin3', 'txttin3', 'tinpart3'):
                tin_parts['3'] = val
            elif short_key in ('tin4', 'txttin4', 'branchcode', 'txtbranchcode', 'branch_code', 'branch'):
                tin_parts['branch'] = val
            elif 'tin' in short_key and (re.match(r'^\d{9,14}$', val) or re.match(r'^\d{3}-\d{3}-\d{3}', val)):
                if not tin_full and not any(bad in short_key for bad in excluded_subkeys):
                    tin_full = val

            # --- 2. Detect Primary Taxpayer / Registered Name ---
            if not primary_name and not any(bad in short_key for bad in excluded_subkeys):
                if any(k in short_key for k in (
                    'taxpayername', 'tpname', 'registeredname', 'regname',
                    'corporatename', 'companyname', 'withholdingagentname',
                    'payorname', 'estatename', 'trustname', 'donorname',
                    'nameoftaxpayer', 'clientname', 'businessname'
                )) or short_key in ('name', 'txtname', 'taxpayer', 'txttaxpayer'):
                    primary_name = val

            # --- 3. Detect Individual Name Parts, 'fn' Profile Tag, & Trade Name ---
            if not any(bad in short_key for bad in excluded_subkeys):
                if (any(k in short_key for k in ('lastname', 'surname', 'lname')) or short_key in ('ln', 'txtln')) and not last_name:
                    last_name = val
                elif (any(k in short_key for k in ('firstname', 'fname')) or short_key in ('fn', 'txtfn')) and not first_name:
                    first_name = val
                elif (any(k in short_key for k in ('middlename', 'mname', 'middleinitial')) or short_key in ('mn', 'txtmn', 'mi')) and not middle_name:
                    middle_name = val
                elif 'tradename' in short_key and not trade_name:
                    trade_name = val

        # Assemble best available Taxpayer Name
        name = primary_name
        if not name:
            if last_name and first_name:
                full_indiv = f"{last_name}, {first_name} {middle_name}".strip()
                name = re.sub(r'\s+', ' ', full_indiv).strip(", ")
            elif first_name:
                # In eBIRForms profile XMLs, 'fn' holds the full company/taxpayer name
                name = re.sub(r'\s+', ' ', first_name).strip()
            elif last_name:
                name = re.sub(r'\s+', ' ', last_name).strip()
        if not name and trade_name:
            name = trade_name

        # Assemble Canonical TIN
        if '1' in tin_parts and '2' in tin_parts and '3' in tin_parts:
            b = tin_parts.get('branch', '000')
            canonical_tin = f"{tin_parts['1']}-{tin_parts['2']}-{tin_parts['3']}-{b}"
        elif tin_full:
            canonical_tin = tin_full
        else:
            match = re.search(r'(\d{9,14})', filename)
            canonical_tin = match.group(1) if match else "Unknown-TIN"
        
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

def extract_form_type(filename, rel_dir=""):
    if 'profile' in rel_dir.lower():
        return None
    name, _ = os.path.splitext(filename)
    parts = re.split(r'[-_]', name)
    for part in parts:
        if re.match(r'^\d{8,14}$', part):
            continue
        if len(part) >= 3 and any(c.isdigit() for c in part):
            return part.upper()
    return None

def transform_filename(filename, mode, custom_suffix=""):
    name, ext = os.path.splitext(filename)
    if mode == "append_00":
        name = re.sub(r'(?<!\d)(\d{12})(?!\d)', r'\g<1>00', name)
    elif mode == "remove_00":
        name = re.sub(r'(?<!\d)(\d{12})00(?!\d)', r'\g<1>', name)
    elif mode == "custom":
        name = re.sub(r'(?<!\d)(\d{12})(?!\d)', r'\g<1>' + custom_suffix, name)
    return name + ext

def apply_tin_transform_to_string(xml_data, mode, custom_suffix=""):
    if mode == "none":
        return xml_data

    def transform_val(short_key, val):
        is_branch = any(k in short_key for k in ('branchcode', 'branch_code', 'tin4', 'branch', 'txtbranchcode'))
        if mode == "append_00":
            if is_branch and len(val) == 3 and val.isdigit(): return val + "00"
            elif re.match(r'^\d{12}$', val): return val + "00"
            elif re.match(r'^\d{3}-\d{3}-\d{3}-\d{3}$', val): return val + "00"
        elif mode == "remove_00":
            if is_branch and len(val) == 5 and val.endswith("00"): return val[:-2]
            elif re.match(r'^\d{14}$', val) and val.endswith("00"): return val[:-2]
            elif re.match(r'^\d{3}-\d{3}-\d{3}-\d{5}$', val) and val.endswith("00"): return val[:-2]
        elif mode == "custom":
            if is_branch and len(val) == 3 and val.isdigit(): return val + custom_suffix
            elif re.match(r'^\d{12}$', val): return val + custom_suffix
            elif re.match(r'^\d{3}-\d{3}-\d{3}-\d{3}$', val): return val + custom_suffix
        return val

    # 1. Transform eBIRForms <div>key=valuekey=</div> blocks
    def div_replacer(match):
        prefix, raw_key, val, suffix = match.groups()
        short_key = raw_key.split(':')[-1].lower()
        new_val = transform_val(short_key, val.strip())
        return f"{prefix}{new_val}{suffix}"

    xml_data = re.sub(
        r'(<div[^>]*>\s*([a-zA-Z0-9_:-]+)=)(.*?)(\2=\s*</div>)',
        div_replacer, xml_data, flags=re.IGNORECASE | re.DOTALL
    )

    # 2. Transform standard <tag>value</tag> XML nodes
    def tag_replacer(match):
        open_tag, tag_name, text, close_tag = match.groups()
        if tag_name.lower() == 'div':
            return match.group(0)
        short_key = tag_name.split(':')[-1].lower()
        new_text = transform_val(short_key, text.strip())
        return f"{open_tag}{new_text}{close_tag}"

    xml_data = re.sub(
        r'(<([a-zA-Z0-9_:-]+)[^>]*>)([^<]+)(</[a-zA-Z0-9_:-]+>)',
        tag_replacer, xml_data, flags=re.IGNORECASE
    )

    return xml_data

def apply_date_transform_to_string(xml_data, new_month, new_year):
    last_day = calendar.monthrange(new_year, new_month)[1]
    mm = f"{new_month:02d}"
    yyyy = str(new_year)
    mm_dd_yyyy = f"{mm}/{last_day}/{yyyy}"

    # 1. Update <div>key=valuekey=</div> date fields (e.g., frm1601C:txtMonth=10)
    def div_date_replacer(match):
        prefix, raw_key, val, suffix = match.groups()
        short_key = raw_key.split(':')[-1].lower()
        if re.match(r'^(txt)?(forthe)?month\d*$', short_key):
            return f"{prefix}{mm}{suffix}"
        elif re.match(r'^(txt)?year\d*$', short_key):
            return f"{prefix}{yyyy}{suffix}"
        elif 'returnperiod' in short_key:
            return f"{prefix}{mm_dd_yyyy}{suffix}"
        return match.group(0)

    xml_data = re.sub(
        r'(<div[^>]*>\s*([a-zA-Z0-9_:-]+)=)(.*?)(\2=\s*</div>)',
        div_date_replacer, xml_data, flags=re.IGNORECASE | re.DOTALL
    )

    # 2. Update standard XML date nodes (<ReturnPeriod>, <month>, <year>)
    xml_data = re.sub(r'(<([a-zA-Z0-9_:-]*month\d*)[^>]*>)([^<]+)(</\2>)', 
                      lambda m: f"{m.group(1)}{mm}{m.group(4)}", xml_data, flags=re.IGNORECASE)
    xml_data = re.sub(r'(<([a-zA-Z0-9_:-]*year\d*)[^>]*>)([^<]+)(</\2>)', 
                      lambda m: f"{m.group(1)}{yyyy}{m.group(4)}", xml_data, flags=re.IGNORECASE)
    xml_data = re.sub(r'(<([a-zA-Z0-9_:-]*returnperiod)[^>]*>)([^<]+)(</\2>)', 
                      lambda m: f"{m.group(1)}{mm_dd_yyyy}{m.group(4)}", xml_data, flags=re.IGNORECASE)
    return xml_data

def generate_cloned_filename(original_filename, new_month, new_year, mode, custom_suffix):
    last_day = calendar.monthrange(new_year, new_month)[1]
    mm = f"{new_month:02d}"
    yyyy = str(new_year)
    mmddyyyy = f"{mm}{last_day}{yyyy}"

    name = transform_filename(original_filename, mode, custom_suffix)
    if re.match(r'^\d{8}-', name):
        name = re.sub(r'^\d{8}', mmddyyyy, name)
    elif re.search(r'-\d{8}\.xml$', name, re.IGNORECASE):
        name = re.sub(r'-\d{8}(\.xml)$', f'-{mmddyyyy}\\1', name, flags=re.IGNORECASE)
    else:
        name = f"{mmddyyyy}-{name}"
    return name

def transform_xml_file(source_path, dest_path, mode, custom_suffix=""):
    with open(source_path, 'r', encoding='utf-8', errors='ignore') as f:
        xml_data = f.read()
    xml_data = apply_tin_transform_to_string(xml_data, mode, custom_suffix)
    with open(dest_path, 'w', encoding='utf-8') as f:
        f.write(xml_data)

# ==============================================================================
# AUTO-BACKUP & SELECTIVE REVERT MANAGER
# ==============================================================================

def create_taxpayer_backup(target_root, taxpayer_rec, operation_label, newly_created_rel_paths=None):
    backup_base = os.path.join(target_root, BACKUP_DIR_NAME, taxpayer_rec['base_tin'])
    os.makedirs(backup_base, exist_ok=True)
    files_dir = os.path.join(backup_base, "files")

    if os.path.exists(files_dir):
        shutil.rmtree(files_dir)
    os.makedirs(files_dir, exist_ok=True)

    backed_up_files = []
    for f in taxpayer_rec.get('files', []):
        if not os.path.exists(f['path']):
            continue
        rel_dir = f.get('rel_dir', '')
        rel_path = os.path.join(rel_dir, f['filename']) if rel_dir else f['filename']
        dest_backup_file = os.path.join(files_dir, rel_path)
        os.makedirs(os.path.dirname(dest_backup_file), exist_ok=True)
        shutil.copy2(f['path'], dest_backup_file)
        backed_up_files.append({
            'rel_path': rel_path,
            'mtime': f['mtime']
        })

    meta = {
        'base_tin': taxpayer_rec['base_tin'],
        'tin': taxpayer_rec['tin'],
        'name': taxpayer_rec['name'],
        'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        'operation': operation_label,
        'original_files': backed_up_files,
        'created_files': newly_created_rel_paths or []
    }

    with open(os.path.join(backup_base, "meta.json"), 'w', encoding='utf-8') as f_out:
        json.dump(meta, f_out, indent=2)

def register_created_files_in_backup(target_root, base_tin, created_rel_paths):
    meta_path = os.path.join(target_root, BACKUP_DIR_NAME, base_tin, "meta.json")
    if os.path.exists(meta_path):
        try:
            with open(meta_path, 'r', encoding='utf-8') as f:
                meta = json.load(f)
            existing = set(meta.get('created_files', []))
            existing.update(created_rel_paths)
            meta['created_files'] = list(existing)
            with open(meta_path, 'w', encoding='utf-8') as f:
                json.dump(meta, f, indent=2)
        except Exception:
            pass

def get_available_backups(target_root):
    backups = {}
    backup_root = os.path.join(target_root, BACKUP_DIR_NAME)
    if not os.path.isdir(backup_root):
        return backups

    for entry in os.listdir(backup_root):
        meta_path = os.path.join(backup_root, entry, "meta.json")
        if os.path.isfile(meta_path):
            try:
                with open(meta_path, 'r', encoding='utf-8') as f:
                    meta = json.load(f)
                backups[meta['base_tin']] = meta
            except Exception:
                continue
    return backups

def restore_taxpayer_backup(target_root, base_tin):
    backup_folder = os.path.join(target_root, BACKUP_DIR_NAME, base_tin)
    meta_path = os.path.join(backup_folder, "meta.json")
    files_dir = os.path.join(backup_folder, "files")

    if not os.path.isfile(meta_path):
        return 0

    with open(meta_path, 'r', encoding='utf-8') as f:
        meta = json.load(f)

    orig_rel_set = {item['rel_path'] for item in meta.get('original_files', [])}

    for created_rel in meta.get('created_files', []):
        if created_rel not in orig_rel_set:
            created_full = os.path.join(target_root, created_rel)
            if os.path.isfile(created_full):
                try:
                    os.remove(created_full)
                except Exception:
                    pass

    restored_count = 0
    for item in meta.get('original_files', []):
        rel_path = item['rel_path']
        src_backup = os.path.join(files_dir, rel_path)
        dest_original = os.path.join(target_root, rel_path)
        if os.path.isfile(src_backup):
            os.makedirs(os.path.dirname(dest_original), exist_ok=True)
            shutil.copy2(src_backup, dest_original)
            mtime = item.get('mtime')
            if mtime:
                os.utime(dest_original, (mtime, mtime))
            restored_count += 1

    shutil.rmtree(backup_folder, ignore_errors=True)
    return restored_count

# ==============================================================================
# MODAL DIALOGS
# ==============================================================================

class ConflictDialog(QDialog):
    RES_OVERWRITE, RES_SKIP, RES_OVERWRITE_ALL, RES_SKIP_ALL = 1, 2, 3, 4

    def __init__(self, taxpayer_name, tin, src_date, dest_date, is_dest_newer, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Conflict Detected")
        self.setFixedSize(500, 260)
        self.user_choice = self.RES_SKIP
        layout = QVBoxLayout(self)

        info = QLabel(f"<b>Taxpayer:</b> {taxpayer_name}<br><b>TIN:</b> {tin}<br><br>"
                      f"<b>Source Record Last Update:</b> {src_date}<br>"
                      f"<b>Destination (Existing) Last Update:</b> {dest_date}")
        info.setTextFormat(Qt.RichText)
        layout.addWidget(info)

        warn = QLabel("⚠️ The EXISTING record is NEWER." if is_dest_newer else "ℹ️ The SOURCE record is NEWER.")
        warn.setStyleSheet("color: #D32F2F; font-weight: bold;" if is_dest_newer else "color: #1976D2; font-weight: bold;")
        layout.addWidget(warn)

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


class BatchCloneDialog(QDialog):
    def __init__(self, taxpayer_list, dest_folder, dest_records, tin_mode, custom_suffix, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Batch Clone Returns ({len(taxpayer_list)} Taxpayer(s) Selected)")
        self.resize(820, 520)
        self.taxpayer_list = taxpayer_list
        self.dest_folder = dest_folder
        self.dest_records = dest_records
        self.tin_mode = tin_mode
        self.custom_suffix = custom_suffix

        layout = QVBoxLayout(self)

        top_box = QGroupBox("Clone Settings")
        top_layout = QHBoxLayout(top_box)

        top_layout.addWidget(QLabel("<b>Form Type:</b>"))
        self.form_type_combo = QComboBox()
        self.populate_form_types()
        self.form_type_combo.currentIndexChanged.connect(self.refresh_preview_table)
        top_layout.addWidget(self.form_type_combo)

        top_layout.addSpacing(15)
        top_layout.addWidget(QLabel("<b>Target Month:</b>"))
        self.month_combo = QComboBox()
        self.month_combo.addItems([f"{i:02d} - {calendar.month_name[i]}" for i in range(1, 13)])
        self.month_combo.setCurrentIndex(datetime.now().month - 1)
        self.month_combo.currentIndexChanged.connect(self.refresh_preview_table)
        top_layout.addWidget(self.month_combo)

        top_layout.addSpacing(15)
        top_layout.addWidget(QLabel("<b>Target Year:</b>"))
        self.year_input = QLineEdit(str(datetime.now().year))
        self.year_input.setMaximumWidth(80)
        self.year_input.textChanged.connect(self.refresh_preview_table)
        top_layout.addWidget(self.year_input)

        layout.addWidget(top_box)

        layout.addWidget(QLabel("<b>Preview of Returns to Generate (Uncheck any you want to skip):</b>"))
        self.preview_table = QTableWidget(0, 5)
        self.preview_table.setHorizontalHeaderLabels([
            "Clone", "Taxpayer Name", "TIN", "Template File (Latest Match)", "New Return Filename"
        ])
        self.preview_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.preview_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.preview_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.preview_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        layout.addWidget(self.preview_table)

        btn_layout = QHBoxLayout()
        self.gen_btn = QPushButton("✅ Backup & Generate Selected Cloned Returns")
        self.gen_btn.setStyleSheet("background-color: #F57C00; color: white; font-weight: bold; padding: 10px;")
        self.gen_btn.clicked.connect(self.execute_batch_clone)
        btn_layout.addWidget(self.gen_btn)
        layout.addLayout(btn_layout)

        self.refresh_preview_table()

    def populate_form_types(self):
        form_types = set()
        for rec in self.taxpayer_list:
            for f in rec['files']:
                ft = extract_form_type(f['filename'], f.get('rel_dir', ''))
                if ft:
                    form_types.add(ft)
        sorted_forms = sorted(form_types)
        for ft in sorted_forms:
            self.form_type_combo.addItem(f"BIR Form {ft}", ft)
        self.form_type_combo.addItem("Latest Return (Any Form Type)", "ANY")

    def get_matching_template(self, rec, selected_form_type):
        candidates = []
        for f in rec['files']:
            if 'profile' in f.get('rel_dir', '').lower():
                continue
            ft = extract_form_type(f['filename'], f.get('rel_dir', ''))
            if selected_form_type == "ANY" or ft == selected_form_type:
                candidates.append(f)
        if not candidates:
            return None
        return max(candidates, key=lambda x: x['mtime'])

    def refresh_preview_table(self):
        self.preview_table.setRowCount(0)
        selected_ft = self.form_type_combo.currentData()
        month = self.month_combo.currentIndex() + 1
        try:
            year = int(self.year_input.text().strip())
            if year < 1990 or year > 2100: raise ValueError
        except ValueError:
            return

        for rec in self.taxpayer_list:
            r = self.preview_table.rowCount()
            self.preview_table.insertRow(r)

            template_file = self.get_matching_template(rec, selected_ft)
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)

            if template_file:
                new_fname = generate_cloned_filename(
                    template_file['filename'], month, year, self.tin_mode, self.custom_suffix
                )
                chk.setCheckState(Qt.Checked)
                chk.setData(Qt.UserRole, (rec, template_file, new_fname))
                tpl_text = template_file['filename']
            else:
                chk.setCheckState(Qt.Unchecked)
                chk.setFlags(Qt.NoItemFlags)
                chk.setData(Qt.UserRole, None)
                tpl_text = f"No {selected_ft} return found"
                new_fname = "—"

            self.preview_table.setItem(r, 0, chk)
            self.preview_table.setItem(r, 1, QTableWidgetItem(rec['name']))
            self.preview_table.setItem(r, 2, QTableWidgetItem(rec['tin']))
            self.preview_table.setItem(r, 3, QTableWidgetItem(tpl_text))
            self.preview_table.setItem(r, 4, QTableWidgetItem(new_fname))

    def execute_batch_clone(self):
        month = self.month_combo.currentIndex() + 1
        try:
            year = int(self.year_input.text().strip())
        except ValueError:
            return QMessageBox.warning(self, "Error", "Please enter a valid 4-digit Target Year.")

        cloned_count = 0
        for r in range(self.preview_table.rowCount()):
            chk = self.preview_table.item(r, 0)
            if not chk or chk.checkState() != Qt.Checked:
                continue
            data = chk.data(Qt.UserRole)
            if not data:
                continue

            rec, template_file, new_filename = data
            rel_dir = template_file.get('rel_dir', 'savefile') or 'savefile'
            target_dir = os.path.join(self.dest_folder, rel_dir)
            os.makedirs(target_dir, exist_ok=True)

            dest_path = os.path.join(target_dir, new_filename)
            rel_created = os.path.join(rel_dir, new_filename)

            existing_rec = self.dest_records.get(rec['base_tin'], rec)
            if rec['base_tin'] not in get_available_backups(self.dest_folder):
                create_taxpayer_backup(self.dest_folder, existing_rec, f"Batch Clone ({new_filename})")
            register_created_files_in_backup(self.dest_folder, rec['base_tin'], [rel_created])

            try:
                with open(template_file['path'], 'r', encoding='utf-8', errors='ignore') as f:
                    xml_data = f.read()
                xml_data = apply_tin_transform_to_string(xml_data, self.tin_mode, self.custom_suffix)
                xml_data = apply_date_transform_to_string(xml_data, month, year)
                with open(dest_path, 'w', encoding='utf-8') as f:
                    f.write(xml_data)
                cloned_count += 1
            except Exception as e:
                QMessageBox.warning(self, "Clone Error", f"Failed on {rec['name']}: {e}")

        QMessageBox.information(self, "Batch Clone Complete", f"Successfully generated {cloned_count} cloned return(s)!")
        self.accept()


class RevertDialog(QDialog):
    def __init__(self, target_folder, backups_dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Selective Revert / Restore Taxpayers")
        self.resize(780, 450)
        self.target_folder = target_folder
        self.backups_dict = backups_dict

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"<b>Target eBIRForms Folder:</b> {target_folder}<br>"
            "Select the taxpayers you want to revert to their backed-up state. "
            "Unchecked taxpayers will remain untouched."
        ))

        sel_bar = QHBoxLayout()
        btn_all = QPushButton("Select All")
        btn_none = QPushButton("Deselect All")
        btn_all.clicked.connect(lambda: self.set_all(True))
        btn_none.clicked.connect(lambda: self.set_all(False))
        sel_bar.addWidget(btn_all)
        sel_bar.addWidget(btn_none)
        sel_bar.addStretch()
        layout.addLayout(sel_bar)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels([
            "Revert", "Taxpayer Name", "TIN", "Backup Date", "Last Operation"
        ])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        layout.addWidget(self.table)

        for base_tin, meta in backups_dict.items():
            r = self.table.rowCount()
            self.table.insertRow(r)
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(Qt.Checked)
            chk.setData(Qt.UserRole, base_tin)
            self.table.setItem(r, 0, chk)
            self.table.setItem(r, 1, QTableWidgetItem(meta.get('name', 'Unknown')))
            self.table.setItem(r, 2, QTableWidgetItem(meta.get('tin', base_tin)))
            self.table.setItem(r, 3, QTableWidgetItem(meta.get('timestamp', '')))
            self.table.setItem(r, 4, QTableWidgetItem(meta.get('operation', 'Modified')))

        restore_btn = QPushButton("⏪ Restore Selected Taxpayers to Original State")
        restore_btn.setStyleSheet("background-color: #C62828; color: white; font-weight: bold; padding: 10px;")
        restore_btn.clicked.connect(self.execute_revert)
        layout.addWidget(restore_btn)

    def set_all(self, checked):
        state = Qt.Checked if checked else Qt.Unchecked
        for r in range(self.table.rowCount()):
            if item := self.table.item(r, 0):
                item.setCheckState(state)

    def execute_revert(self):
        selected_tins = [
            self.table.item(r, 0).data(Qt.UserRole)
            for r in range(self.table.rowCount())
            if self.table.item(r, 0) and self.table.item(r, 0).checkState() == Qt.Checked
        ]
        if not selected_tins:
            return QMessageBox.information(self, "No Selection", "Please check at least one taxpayer to revert.")

        if QMessageBox.question(
            self, "Confirm Revert",
            f"Revert {len(selected_tins)} selected taxpayer(s) to their backed-up state?",
            QMessageBox.Yes | QMessageBox.No
        ) == QMessageBox.No:
            return

        total_files = 0
        for b_tin in selected_tins:
            total_files += restore_taxpayer_backup(self.target_folder, b_tin)

        QMessageBox.information(
            self, "Revert Complete",
            f"Successfully reverted {len(selected_tins)} taxpayer(s) ({total_files} original files restored)."
        )
        self.accept()

# ==============================================================================
# MAIN APPLICATION WINDOW
# ==============================================================================

class EBIRManagerApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Xyk0Tech eBIRForms Data Manager")
        self.resize(1150, 760)
        self.source_records = {}       
        self.dest_records = {}         
        self.init_ui()

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        folder_group = QGroupBox("Directory Setup")
        folder_layout = QVBoxLayout(folder_group)

        self.inplace_chk = QCheckBox(
            "Work on the SAME eBIRForms folder (In-Place Mode — Use this when updating TINs or cloning returns without a new eBIR version)"
        )
        self.inplace_chk.setStyleSheet("font-weight: bold; color: #1565C0;")
        self.inplace_chk.toggled.connect(self.toggle_inplace_mode)
        folder_layout.addWidget(self.inplace_chk)

        src_row = QHBoxLayout()
        self.src_label = QLabel("Source / Old eBIR Main Folder:")
        src_row.addWidget(self.src_label)
        self.src_input = QLineEdit()
        self.src_input.textChanged.connect(self.sync_inplace_folder)
        src_btn = QPushButton("Browse...")
        src_btn.clicked.connect(self.browse_source)
        src_row.addWidget(self.src_input)
        src_row.addWidget(src_btn)
        folder_layout.addLayout(src_row)

        dest_row = QHBoxLayout()
        self.dest_label = QLabel("Destination / New eBIR Main Folder:")
        dest_row.addWidget(self.dest_label)
        self.dest_input = QLineEdit()
        self.dest_btn = QPushButton("Browse...")
        self.dest_btn.clicked.connect(self.browse_dest)
        dest_row.addWidget(self.dest_input)
        dest_row.addWidget(self.dest_btn)
        folder_layout.addLayout(dest_row)

        main_layout.addWidget(folder_group)

        ctrl_bar = QHBoxLayout()
        self.scan_btn = QPushButton("🔄 Scan & Compare Records")
        self.scan_btn.setStyleSheet("font-weight: bold; padding: 6px 12px;")
        self.scan_btn.clicked.connect(self.scan_records)
        ctrl_bar.addWidget(self.scan_btn)

        ctrl_bar.addSpacing(20)
        ctrl_bar.addWidget(QLabel("Search/Filter:"))
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Filter by Taxpayer Name or TIN...")
        self.search_input.textChanged.connect(self.filter_table)
        ctrl_bar.addWidget(self.search_input)

        for text, state in [("Select All", True), ("Deselect All", False)]:
            btn = QPushButton(text)
            btn.clicked.connect(lambda checked, s=state: self.set_all_checkboxes(s))
            ctrl_bar.addWidget(btn)
        main_layout.addLayout(ctrl_bar)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels([
            "Select", "Taxpayer / Company Name", "TIN", "Forms Count", "Last Updated", "Status / Backup Info"
        ])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        main_layout.addWidget(self.table)

        tin_box = QGroupBox("TIN Format Options (Updates BOTH Filenames and XML tags)")
        tin_layout = QHBoxLayout(tin_box)
        self.tin_combo = QComboBox()
        self.tin_combo.addItem("Convert 3-digit to 5-digit Branch Code (Append '00')", "append_00")
        self.tin_combo.addItem("Keep Original / No TIN Transformation", "none")
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

        action_layout = QGridLayout()
        
        self.upgrade_btn = QPushButton("⚡ Quick Upgrade: 000 \u2192 00000")
        self.upgrade_btn.setStyleSheet("background-color: #0288D1; color: white; font-weight: bold; padding: 10px;")
        self.upgrade_btn.clicked.connect(self.quick_upgrade_tins)

        self.transfer_btn = QPushButton("🚀 Transfer / Update Selected Taxpayers")
        self.transfer_btn.setStyleSheet("background-color: #2E7D32; color: white; font-weight: bold; padding: 10px;")
        self.transfer_btn.clicked.connect(self.transfer_selected)

        self.clone_btn = QPushButton("📑 Clone Returns (Batch Monthly Template)")
        self.clone_btn.setStyleSheet("background-color: #F57C00; color: white; font-weight: bold; padding: 10px;")
        self.clone_btn.clicked.connect(self.open_clone_dialog)

        self.revert_btn = QPushButton("⏪ Revert / Restore")
        self.revert_btn.setStyleSheet("background-color: #C62828; color: white; font-weight: bold; padding: 10px;")
        self.revert_btn.clicked.connect(self.open_revert_dialog)

        self.export_btn = QPushButton("💾 Export Backup Package")
        self.export_btn.setStyleSheet("background-color: #1565C0; color: white; font-weight: bold; padding: 10px;")
        self.export_btn.clicked.connect(self.export_backup)

        self.import_btn = QPushButton("📂 Import Backup")
        self.import_btn.setStyleSheet("background-color: #6A1B9A; color: white; font-weight: bold; padding: 10px;")
        self.import_btn.clicked.connect(self.import_backup)

        action_layout.addWidget(self.upgrade_btn, 0, 0)
        action_layout.addWidget(self.transfer_btn, 0, 1)
        action_layout.addWidget(self.clone_btn, 0, 2)
        action_layout.addWidget(self.revert_btn, 1, 0)
        action_layout.addWidget(self.export_btn, 1, 1)
        action_layout.addWidget(self.import_btn, 1, 2)
        main_layout.addLayout(action_layout)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(110)
        main_layout.addWidget(self.log_view)

    def toggle_inplace_mode(self, checked):
        if checked:
            self.dest_input.setText(self.src_input.text())
            self.dest_input.setEnabled(False)
            self.dest_btn.setEnabled(False)
            self.src_label.setText("Active eBIRForms Main Folder:")
        else:
            self.dest_input.setEnabled(True)
            self.dest_btn.setEnabled(True)
            self.src_label.setText("Source / Old eBIR Main Folder:")

    def sync_inplace_folder(self, text):
        if self.inplace_chk.isChecked():
            self.dest_input.setText(text)

    def is_inplace(self):
        src = os.path.normcase(os.path.abspath(self.src_input.text().strip())) if self.src_input.text().strip() else ""
        dest = os.path.normcase(os.path.abspath(self.dest_input.text().strip())) if self.dest_input.text().strip() else ""
        return self.inplace_chk.isChecked() or (src and src == dest)

    def browse_source(self):
        if folder := QFileDialog.getExistingDirectory(self, "Select eBIRForms Main Folder"):
            self.src_input.setText(folder)

    def browse_dest(self):
        if folder := QFileDialog.getExistingDirectory(self, "Select Destination eBIRForms Main Folder"):
            self.dest_input.setText(folder)

    def toggle_custom_suffix(self):
        self.custom_suffix_input.setEnabled(self.tin_combo.currentData() == "custom")

    def log(self, text):
        self.log_view.append(f"[{datetime.now().strftime('%H:%M:%S')}] {text}")
        self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())
        QApplication.processEvents()

    def scan_records(self):
        src = self.src_input.text().strip()
        dest = self.dest_input.text().strip()
        if not os.path.isdir(src):
            return QMessageBox.warning(self, "Error", "Please select a valid Source / Active eBIRForms folder.")

        if not dest:
            self.inplace_chk.setChecked(True)
            dest = src

        self.log_view.clear()
        self.source_records = self._read_records(src)
        self.dest_records = self._read_records(dest) if os.path.isdir(dest) else {}
        self.populate_table()
        self.log(f"Scan complete. Found {len(self.source_records)} unique taxpayer profiles.")

    def _read_records(self, folder):
        grouped = {}
        for root_dir, dirs, files in os.walk(folder):
            if BACKUP_DIR_NAME in dirs:
                dirs.remove(BACKUP_DIR_NAME)

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
                            is_valid_name = rec['name'] != "Unknown Taxpayer" and "Recovered" not in rec['name']
                            is_profile_file = 'profile' in rel_dir.lower()

                            if b_tin not in grouped:
                                grouped[b_tin] = {
                                    'name': rec['name'], 'tin': rec['tin'], 'base_tin': b_tin,
                                    'files': [], 'latest_mtime': rec['mtime'], 'latest_str': rec['mtime_str']
                                }
                            else:
                                if is_valid_name and (grouped[b_tin]['name'] == "Unknown Taxpayer" or is_profile_file):
                                    grouped[b_tin]['name'] = rec['name']

                            grouped[b_tin]['files'].append(rec)

                            if rec['mtime'] >= grouped[b_tin]['latest_mtime']:
                                grouped[b_tin].update({
                                    'latest_mtime': rec['mtime'],
                                    'latest_str': rec['mtime_str'],
                                    'tin': rec['tin']
                                })
                                if is_valid_name:
                                    grouped[b_tin]['name'] = rec['name']
                    except Exception as e:
                        self.log(f"⚠️ Skipped {file}: {str(e)}")
        return grouped

    def populate_table(self):
        self.table.setRowCount(0)
        self.table.setSortingEnabled(False)
        dest_folder = self.dest_input.text().strip() or self.src_input.text().strip()
        backups = get_available_backups(dest_folder) if dest_folder else {}
        inplace = self.is_inplace()

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
            has_bak = " [Backup Ready]" if base_tin in backups else ""

            if inplace:
                clean_digits = re.sub(r'[^0-9]', '', rec['tin'])
                if len(clean_digits) >= 14:
                    dest_item.setText(f"In-Place (5-Digit Branch){has_bak}")
                    dest_item.setForeground(QColor("#388E3C"))
                else:
                    dest_item.setText(f"In-Place (3-Digit Branch){has_bak}")
                    dest_item.setForeground(QColor("#F57C00"))
            else:
                if base_tin in self.dest_records:
                    d_rec = self.dest_records[base_tin]
                    if d_rec['latest_mtime'] > rec['latest_mtime']:
                        dest_item.setText(f"Exists (Dest is NEWER){has_bak}")
                        dest_item.setForeground(QColor("#D32F2F"))
                    elif d_rec['latest_mtime'] < rec['latest_mtime']:
                        dest_item.setText(f"Exists (Source is NEWER){has_bak}")
                        dest_item.setForeground(QColor("#1976D2"))
                    else:
                        dest_item.setText(f"Exists (Same timestamp){has_bak}")
                        dest_item.setForeground(QColor("#388E3C"))
                else:
                    dest_item.setText(f"New Record{has_bak}")
                    dest_item.setForeground(QColor("#388E3C"))

            self.table.setItem(r, 5, dest_item)
        self.table.setSortingEnabled(True)

    def filter_table(self, q):
        q = q.lower()
        for r in range(self.table.rowCount()):
            self.table.setRowHidden(
                r, q not in self.table.item(r, 1).text().lower() and q not in self.table.item(r, 2).text().lower()
            )

    def set_all_checkboxes(self, c):
        for r in range(self.table.rowCount()):
            if i := self.table.item(r, 0):
                i.setCheckState(Qt.Checked if c else Qt.Unchecked)

    def get_selected_tins(self):
        return [
            self.table.item(r, 0).data(Qt.UserRole)
            for r in range(self.table.rowCount())
            if self.table.item(r, 0) and self.table.item(r, 0).checkState() == Qt.Checked
        ]

    # --- DEDICATED QUICK UPGRADE ---
    def quick_upgrade_tins(self):
        src = self.src_input.text().strip()
        tins = self.get_selected_tins()
        
        if not src or not os.path.isdir(src):
            return QMessageBox.warning(self, "Error", "Please scan a valid folder first.")
        if not tins:
            return QMessageBox.warning(self, "Error", "Select at least one taxpayer from the table.")
            
        msg = ("This will upgrade the selected records from 3-digit to 5-digit branch codes (000 ➔ 00000) "
               "directly in your current folder.\n\n"
               "This automatically targets all XML files hiding inside 'profile', 'savefile', and 'IAF_RDO_Copy'.\n\n"
               "Continue?")
        if QMessageBox.question(self, "Confirm Upgrade", msg, QMessageBox.Yes | QMessageBox.No) == QMessageBox.No:
            return
            
        t_cnt = 0
        for b_tin in tins:
            s_rec = self.source_records[b_tin]
            create_taxpayer_backup(src, s_rec, "Quick Upgrade (000 to 00000)")
            
            created_rel_paths = []
            for f in s_rec['files']:
                rel_dir = f['rel_dir']
                target_dir = os.path.join(src, rel_dir)
                os.makedirs(target_dir, exist_ok=True)

                new_filename = transform_filename(f['filename'], "append_00")
                dest_file_path = os.path.join(target_dir, new_filename)
                rel_created = os.path.join(rel_dir, new_filename) if rel_dir else new_filename

                try:
                    transform_xml_file(f['path'], dest_file_path, "append_00")
                    created_rel_paths.append(rel_created)
                    
                    if os.path.normcase(f['path']) != os.path.normcase(dest_file_path):
                        if os.path.exists(f['path']):
                            os.remove(f['path'])
                    t_cnt += 1
                except Exception as e:
                    self.log(f"Error upgrading {f['filename']}: {e}")
                    
            if created_rel_paths:
                register_created_files_in_backup(src, b_tin, created_rel_paths)
            self.log(f"Upgraded: {s_rec['name']}")

        QMessageBox.information(self, "Complete", f"Upgrade Complete!\nSuccessfully updated {t_cnt} files.\nSafety backups were created.")
        self.scan_records()

    # --- BATCH CLONING ---
    def open_clone_dialog(self):
        if not self.source_records:
            return QMessageBox.warning(self, "Scan First", "Please scan your eBIRForms folder first.")
        dest = self.dest_input.text().strip() or self.src_input.text().strip()
        tins = self.get_selected_tins()
        if not tins:
            return QMessageBox.information(self, "Selection Required", "Please check at least one taxpayer to clone.")

        selected_records = [self.source_records[t] for t in tins if t in self.source_records]
        mode, suffix = self.tin_combo.currentData(), self.custom_suffix_input.text().strip()

        dlg = BatchCloneDialog(selected_records, dest, self.dest_records, mode, suffix, self)
        if dlg.exec():
            self.scan_records()

    # --- SELECTIVE REVERT ---
    def open_revert_dialog(self):
        target_folder = self.dest_input.text().strip() or self.src_input.text().strip()
        if not target_folder or not os.path.isdir(target_folder):
            return QMessageBox.warning(self, "Folder Required", "Please select your eBIRForms folder first.")

        backups = get_available_backups(target_folder)
        if not backups:
            return QMessageBox.information(
                self, "No Backups Found",
                "No automatic backups were found in this eBIRForms folder yet.\n"
                "(Backups are automatically created before updating, overwriting, or cloning records.)"
            )

        dlg = RevertDialog(target_folder, backups, self)
        if dlg.exec():
            self.scan_records()

    # --- TRANSFER / IN-PLACE UPDATE LOGIC ---
    def transfer_selected(self):
        src = self.src_input.text().strip()
        dest = self.dest_input.text().strip() or src
        tins = self.get_selected_tins()
        if not dest or not tins:
            return QMessageBox.warning(self, "Error", "Please scan and select taxpayers first.")
        os.makedirs(dest, exist_ok=True)

        inplace = self.is_inplace()
        mode, suffix = self.tin_combo.currentData(), self.custom_suffix_input.text().strip()
        ow_all, skip_all, t_cnt, s_cnt = False, False, 0, 0

        for b_tin in tins:
            s_rec = self.source_records[b_tin]

            if not inplace and b_tin in self.dest_records:
                if not ow_all and not skip_all:
                    dlg = ConflictDialog(
                        s_rec['name'], s_rec['tin'], s_rec['latest_str'],
                        self.dest_records[b_tin]['latest_str'],
                        self.dest_records[b_tin]['latest_mtime'] > s_rec['latest_mtime'], self
                    )
                    dlg.exec()
                    if dlg.user_choice == ConflictDialog.RES_SKIP: s_cnt += 1; continue
                    elif dlg.user_choice == ConflictDialog.RES_SKIP_ALL: skip_all = True; s_cnt += 1; continue
                    elif dlg.user_choice == ConflictDialog.RES_OVERWRITE_ALL: ow_all = True
                elif skip_all:
                    s_cnt += 1
                    continue

            if inplace:
                create_taxpayer_backup(dest, s_rec, "In-Place TIN Update")
            elif b_tin in self.dest_records:
                create_taxpayer_backup(dest, self.dest_records[b_tin], "Pre-Migration Overwrite Backup")

            created_rel_paths = []
            for f in s_rec['files']:
                rel_dir = f['rel_dir']
                target_dir = os.path.join(dest, rel_dir)
                os.makedirs(target_dir, exist_ok=True)

                new_filename = transform_filename(f['filename'], mode, suffix)
                dest_file_path = os.path.join(target_dir, new_filename)
                rel_created = os.path.join(rel_dir, new_filename) if rel_dir else new_filename

                try:
                    transform_xml_file(f['path'], dest_file_path, mode, suffix)
                    created_rel_paths.append(rel_created)
                    if inplace and os.path.normcase(f['path']) != os.path.normcase(dest_file_path):
                        if os.path.exists(f['path']):
                            os.remove(f['path'])
                    t_cnt += 1
                except Exception as e:
                    self.log(f"Error processing {f['filename']}: {e}")

            if created_rel_paths:
                register_created_files_in_backup(dest, b_tin, created_rel_paths)
            self.log(f"{'Updated in-place' if inplace else 'Transferred'}: {s_rec['name']} (Auto-backup saved)")

        QMessageBox.information(
            self, "Complete",
            f"Operation Complete!\nFiles processed: {t_cnt}\nTaxpayers skipped: {s_cnt}\n\n"
            f"Safety backups were automatically saved. You can undo changes anytime using 'Revert / Restore'."
        )
        self.scan_records()

    # --- EXPORT / IMPORT PORTABLE BACKUP ---
    def export_backup(self):
        tins = self.get_selected_tins()
        if not tins:
            return QMessageBox.information(self, "Error", "No records selected.")
        path, _ = QFileDialog.getSaveFileName(self, "Save Backup", "ebir_backup.ebirbak", "Backup (*.ebirbak *.zip)")
        if not path:
            return

        mode, suffix = self.tin_combo.currentData(), self.custom_suffix_input.text().strip()
        manifest = {'created_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 'records': []}

        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
            for b_tin in tins:
                rec = self.source_records[b_tin]
                entry = {
                    'base_tin': b_tin, 'tin': rec['tin'], 'name': rec['name'],
                    'latest_mtime': rec['latest_mtime'], 'latest_str': rec['latest_str'], 'files': []
                }
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
            
        self.log(f"Exported portable backup to {path}")
        QMessageBox.information(self, "Success", "Portable backup exported successfully!")

    def import_backup(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Backup", "", "Backup (*.ebirbak *.zip)")
        if not path:
            return
        dest = self.dest_input.text().strip() or self.src_input.text().strip() or QFileDialog.getExistingDirectory(self, "Select eBIRForms Folder")
        if not dest:
            return
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
                            dlg = ConflictDialog(
                                rec['name'], rec['tin'], rec['latest_str'],
                                self.dest_records[b_tin]['latest_str'],
                                self.dest_records[b_tin]['latest_mtime'] > rec['latest_mtime'], self
                            )
                            dlg.exec()
                            if dlg.user_choice == ConflictDialog.RES_SKIP: s_cnt += 1; continue
                            elif dlg.user_choice == ConflictDialog.RES_SKIP_ALL: skip_all = True; s_cnt += 1; continue
                            elif dlg.user_choice == ConflictDialog.RES_OVERWRITE_ALL: ow_all = True
                        elif skip_all:
                            s_cnt += 1
                            continue

                        create_taxpayer_backup(dest, self.dest_records[b_tin], "Pre-Import Backup")

                    created_rel = []
                    for f in rec['files']:
                        rel_dir = f.get('rel_dir', '')
                        target_dir = os.path.join(dest, rel_dir)
                        os.makedirs(target_dir, exist_ok=True)
                        dest_f = os.path.join(target_dir, f['filename'])
                        
                        with open(dest_f, 'wb') as out:
                            out.write(z.read(f['arcname']))
                        mtime = f.get('mtime', datetime.now().timestamp())
                        os.utime(dest_f, (mtime, mtime))
                        created_rel.append(os.path.join(rel_dir, f['filename']) if rel_dir else f['filename'])
                        i_cnt += 1

                    if b_tin in self.dest_records and created_rel:
                        register_created_files_in_backup(dest, b_tin, created_rel)
                
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