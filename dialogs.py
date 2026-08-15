# -*- coding: utf-8 -*-

import csv

from qgis.PyQt.QtCore import QDate, Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .api import GarminApiError, GarminV1Client, GarminV2Client
from .settings import normalize_track_color


class SettingsDialog(QDialog):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self._project_selection = None
        self.setWindowTitle('Inreach2QGIS settings')
        self.resize(760, 600)

        root = QVBoxLayout(self)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)

        self._build_connection_tab()
        self._build_devices_tab()
        self._build_project_tab()
        self.tabs.currentChanged.connect(self._on_tab_changed)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._load()

    def _build_connection_tab(self):
        tab = QWidget()
        form = QFormLayout(tab)

        self.protocol = QComboBox()
        self.protocol.addItem('IPC Inbound V1 — Basic login/password', 'v1')
        self.protocol.addItem('IPC Inbound V2 — X-API-Key', 'v2')
        self.protocol.currentIndexChanged.connect(self._update_protocol_ui)
        form.addRow('Protocol:', self.protocol)

        self.v1_endpoint = QLineEdit()
        self.v1_username = QLineEdit()
        self.v1_password = QLineEdit()
        self.v1_password.setEchoMode(QLineEdit.Password)
        form.addRow('V1 endpoint:', self.v1_endpoint)
        form.addRow('V1 login:', self.v1_username)
        form.addRow('V1 password:', self.v1_password)

        self.v2_endpoint = QLineEdit()
        self.v2_api_key = QLineEdit()
        self.v2_api_key.setEchoMode(QLineEdit.Password)
        form.addRow('V2 endpoint:', self.v2_endpoint)
        form.addRow('V2 API key:', self.v2_api_key)

        self.refresh_seconds = QSpinBox()
        self.refresh_seconds.setRange(15, 3600)
        self.fresh_minutes = QSpinBox()
        self.fresh_minutes.setRange(1, 1440)
        self.stale_minutes = QSpinBox()
        self.stale_minutes.setRange(1, 10080)
        form.addRow('Refresh every (seconds):', self.refresh_seconds)
        form.addRow('Fresh up to (minutes):', self.fresh_minutes)
        form.addRow('Stale after (minutes):', self.stale_minutes)

        self.test_button = QPushButton('Test connection')
        self.test_button.clicked.connect(self._test_connection)
        form.addRow('', self.test_button)
        self.tabs.addTab(tab, 'Connection')

    def _build_devices_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addWidget(QLabel('Global device registry. This list is available to every QGIS project.'))

        self.devices = QTableWidget(0, 3)
        self.devices.setHorizontalHeaderLabels(['Display name', 'IMEI', 'Track color'])
        self.devices.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.devices.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.devices.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.devices.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.devices.cellDoubleClicked.connect(self._pick_device_color)
        self.devices.cellChanged.connect(self._on_device_cell_changed)
        layout.addWidget(self.devices)

        color_note = QLabel('Double-click a Track color cell to choose the colour used for that device history.')
        color_note.setWordWrap(True)
        layout.addWidget(color_note)

        row = QHBoxLayout()
        for text, callback in (
            ('Add', self._add_device),
            ('Remove selected', self._remove_devices),
            ('Import CSV', self._import_csv),
            ('Export CSV', self._export_csv),
        ):
            button = QPushButton(text)
            button.clicked.connect(callback)
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)
        self.tabs.addTab(tab, 'Devices')

    def _build_project_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        self.project_enabled = QCheckBox('Enable Garmin tracking for this QGIS project')
        layout.addWidget(self.project_enabled)

        self.history_enabled = QCheckBox('Load location history for this project')
        self.history_enabled.toggled.connect(self._update_history_ui)
        layout.addWidget(self.history_enabled)

        history_form = QFormLayout()
        self.history_start = QDateEdit()
        self.history_start.setCalendarPopup(True)
        self.history_start.setDisplayFormat('dd/MM/yyyy')
        self.history_end = QDateEdit()
        self.history_end.setCalendarPopup(True)
        self.history_end.setDisplayFormat('dd/MM/yyyy')
        history_form.addRow('History from:', self.history_start)
        history_form.addRow('History to (inclusive):', self.history_end)
        layout.addLayout(history_form)

        history_note = QLabel(
            'History is fetched once for the selected range when the project/settings are loaded; '
            'the regular refresh timer only updates current positions.'
        )
        history_note.setWordWrap(True)
        layout.addWidget(history_note)

        layout.addWidget(QLabel('Devices for this project:'))
        layout.addWidget(QLabel('Only checked devices will be loaded when this project is open.'))

        self.project_devices = QTableWidget(0, 2)
        self.project_devices.setHorizontalHeaderLabels(['Use', 'Device'])
        self.project_devices.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.project_devices.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.project_devices.setEditTriggers(QAbstractItemView.NoEditTriggers)
        layout.addWidget(self.project_devices)

        row = QHBoxLayout()
        select_all = QPushButton('Select all')
        select_none = QPushButton('Select none')
        select_all.clicked.connect(lambda: self._set_all_project_devices(True))
        select_none.clicked.connect(lambda: self._set_all_project_devices(False))
        row.addWidget(select_all)
        row.addWidget(select_none)
        row.addStretch(1)
        layout.addLayout(row)

        self.project_tab_index = self.tabs.addTab(tab, 'Current project')

    def _load(self):
        idx = self.protocol.findData(self.store.protocol())
        self.protocol.setCurrentIndex(max(0, idx))
        self.v1_endpoint.setText(self.store.endpoint_v1())
        self.v1_username.setText(self.store.v1_username())
        self.v1_password.setText(self.store.v1_password())
        self.v2_endpoint.setText(self.store.endpoint_v2())
        self.v2_api_key.setText(self.store.v2_api_key())
        self.refresh_seconds.setValue(self.store.refresh_seconds())
        self.fresh_minutes.setValue(self.store.fresh_minutes())
        self.stale_minutes.setValue(self.store.stale_minutes())
        self._fill_devices(self.store.devices())
        self.project_enabled.setChecked(self.store.project_enabled())
        self._project_selection = set(self.store.project_imeis())
        self._rebuild_project_devices()

        self.history_enabled.setChecked(self.store.project_history_enabled())
        start_date = QDate.fromString(self.store.project_history_start(), 'yyyy-MM-dd')
        end_date = QDate.fromString(self.store.project_history_end(), 'yyyy-MM-dd')
        today = QDate.currentDate()
        self.history_start.setDate(start_date if start_date.isValid() else today.addDays(-1))
        self.history_end.setDate(end_date if end_date.isValid() else today)

        self._update_protocol_ui()
        self._update_history_ui()

    def _on_tab_changed(self, index):
        if index == self.project_tab_index:
            self._rebuild_project_devices()

    def _update_protocol_ui(self, *args):
        is_v1 = self.protocol.currentData() == 'v1'
        for widget in (self.v1_endpoint, self.v1_username, self.v1_password):
            widget.setEnabled(is_v1)
        for widget in (self.v2_endpoint, self.v2_api_key):
            widget.setEnabled(not is_v1)

    def _update_history_ui(self, *args):
        enabled = self.history_enabled.isChecked()
        self.history_start.setEnabled(enabled)
        self.history_end.setEnabled(enabled)

    def _fill_devices(self, devices):
        self.devices.setRowCount(0)
        for item in devices:
            self._append_device(
                item.get('name', ''),
                item.get('imei', ''),
                item.get('color', ''),
                auto_color=False,
            )

    def _append_device(self, name='', imei='', color='', auto_color=False):
        row = self.devices.rowCount()
        self.devices.insertRow(row)
        self.devices.setItem(row, 0, QTableWidgetItem(str(name)))
        self.devices.setItem(row, 1, QTableWidgetItem(str(imei)))
        self._set_color_item(row, color, auto=auto_color, imei=imei)

    def _set_color_item(self, row, color='', auto=False, imei=''):
        item = self.devices.item(row, 2)
        if item is None:
            item = QTableWidgetItem()
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self.devices.setItem(row, 2, item)

        if auto:
            imei = str(imei or '').strip()
            color = normalize_track_color('', imei) if imei else ''
        else:
            color = normalize_track_color(color, imei)

        item.setData(Qt.UserRole, bool(auto))
        item.setText(color if color else 'AUTO')

        if color:
            qcolor = QColor(color)
            item.setBackground(qcolor)
            item.setForeground(QColor('#ffffff') if qcolor.lightness() < 128 else QColor('#000000'))
        else:
            item.setBackground(QColor('#f0f0f0'))
            item.setForeground(QColor('#555555'))
        item.setTextAlignment(Qt.AlignCenter)

    def _on_device_cell_changed(self, row, column):
        if column != 1:
            return
        color_item = self.devices.item(row, 2)
        if color_item is None or not bool(color_item.data(Qt.UserRole)):
            return
        imei_item = self.devices.item(row, 1)
        imei = imei_item.text().strip() if imei_item else ''
        self._set_color_item(row, auto=True, imei=imei)

    def _pick_device_color(self, row, column):
        if column != 2:
            return
        item = self.devices.item(row, 2)
        if item is None:
            return
        imei_item = self.devices.item(row, 1)
        imei = imei_item.text().strip() if imei_item else ''
        initial = QColor(item.text())
        if not initial.isValid():
            initial = QColor(normalize_track_color('', imei) or '#2563EB')
        chosen = QColorDialog.getColor(initial, self, 'Choose track color')
        if chosen.isValid():
            self._set_color_item(
                row,
                chosen.name().upper(),
                auto=False,
                imei=imei,
            )

    def _add_device(self):
        self._append_device('', '', '', auto_color=True)
        self.devices.setCurrentCell(self.devices.rowCount() - 1, 0)
        self.devices.editItem(self.devices.currentItem())

    def _remove_devices(self):
        for row in sorted({i.row() for i in self.devices.selectedIndexes()}, reverse=True):
            self.devices.removeRow(row)
        self._rebuild_project_devices()

    def _device_rows(self):
        result = []
        seen = set()
        for row in range(self.devices.rowCount()):
            name_item = self.devices.item(row, 0)
            imei_item = self.devices.item(row, 1)
            color_item = self.devices.item(row, 2)
            name = name_item.text().strip() if name_item else ''
            imei = imei_item.text().strip() if imei_item else ''
            if not imei or imei in seen:
                continue
            seen.add(imei)
            if color_item and bool(color_item.data(Qt.UserRole)):
                color = normalize_track_color('', imei)
            else:
                color = color_item.text().strip() if color_item else ''
                color = normalize_track_color(color, imei)
            result.append({
                'name': name or imei,
                'imei': imei,
                'color': color,
            })
        return result

    def _rebuild_project_devices(self):
        # Cache the in-dialog selection independently of the table contents.
        # An empty table is a valid state and must not mean "reload from project".
        if self.project_devices.rowCount():
            self._project_selection = set(self._selected_project_imeis())
        elif self._project_selection is None:
            self._project_selection = set(self.store.project_imeis())

        selected = set(self._project_selection)
        rows = self._device_rows()
        self.project_devices.setRowCount(0)
        for item in rows:
            row = self.project_devices.rowCount()
            self.project_devices.insertRow(row)
            check = QTableWidgetItem('')
            check.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            check.setCheckState(Qt.Checked if item['imei'] in selected else Qt.Unchecked)
            check.setData(Qt.UserRole, item['imei'])
            self.project_devices.setItem(row, 0, check)
            self.project_devices.setItem(row, 1, QTableWidgetItem(f"{item['name']}  ({item['imei']})"))

    def _selected_project_imeis(self):
        result = []
        for row in range(self.project_devices.rowCount()):
            item = self.project_devices.item(row, 0)
            if item and item.checkState() == Qt.Checked:
                result.append(str(item.data(Qt.UserRole)))
        return result

    def _set_all_project_devices(self, checked):
        state = Qt.Checked if checked else Qt.Unchecked
        for row in range(self.project_devices.rowCount()):
            self.project_devices.item(row, 0).setCheckState(state)

    def _import_csv(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            'Import device registry',
            '',
            'CSV files (*.csv);;All files (*)',
        )
        if not path:
            return
        imported = []
        try:
            with open(path, 'r', encoding='utf-8-sig', newline='') as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    name = (row.get('name') or row.get('Name') or '').strip()
                    imei = (row.get('imei') or row.get('IMEI') or '').strip()
                    color = (
                        row.get('color')
                        or row.get('Color')
                        or row.get('track_color')
                        or row.get('Track Color')
                        or ''
                    ).strip()
                    if imei:
                        imported.append({
                            'name': name or imei,
                            'imei': imei,
                            'color': color,
                        })
        except Exception as exc:
            QMessageBox.critical(self, 'Import failed', str(exc))
            return

        merged = {item['imei']: item for item in self._device_rows()}
        for item in imported:
            existing = merged.get(item['imei'])
            if not item.get('color') and existing:
                item['color'] = existing.get('color', '')
            item['color'] = normalize_track_color(item.get('color', ''), item['imei'])
            merged[item['imei']] = item
        self._fill_devices(list(merged.values()))
        self._rebuild_project_devices()

    def _export_csv(self):
        path, _ = QFileDialog.getSaveFileName(
            self,
            'Export device registry',
            'inreach_devices.csv',
            'CSV files (*.csv)',
        )
        if not path:
            return
        if not path.lower().endswith('.csv'):
            path += '.csv'
        try:
            with open(path, 'w', encoding='utf-8-sig', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=['name', 'imei', 'color'])
                writer.writeheader()
                writer.writerows(self._device_rows())
        except Exception as exc:
            QMessageBox.critical(self, 'Export failed', str(exc))

    def _client_from_ui(self):
        if self.protocol.currentData() == 'v1':
            return GarminV1Client(
                self.v1_endpoint.text().strip(),
                self.v1_username.text(),
                self.v1_password.text(),
            )
        return GarminV2Client(
            self.v2_endpoint.text().strip(),
            self.v2_api_key.text(),
        )

    def _test_connection(self):
        devices = self._device_rows()
        if not devices:
            QMessageBox.warning(self, 'Test connection', 'Add at least one device first.')
            return
        try:
            locations = self._client_from_ui().last_known_locations([devices[0]['imei']])
        except GarminApiError as exc:
            QMessageBox.critical(self, 'Test connection', str(exc))
            return
        if locations:
            QMessageBox.information(
                self,
                'Test connection',
                f"Success. Garmin returned {len(locations)} location(s) for {devices[0]['name']}.",
            )
        else:
            QMessageBox.warning(
                self,
                'Test connection',
                'Request succeeded, but Garmin returned no location.',
            )

    def accept(self):
        if self.stale_minutes.value() < self.fresh_minutes.value():
            QMessageBox.warning(
                self,
                'Invalid thresholds',
                'Stale threshold must be greater than or equal to Fresh threshold.',
            )
            return

        if self.history_enabled.isChecked() and self.history_start.date() > self.history_end.date():
            QMessageBox.warning(
                self,
                'Invalid history range',
                'History start date must be before or equal to the end date.',
            )
            return

        devices = self._device_rows()
        selected_imeis = self._selected_project_imeis()

        # Credentials are the only settings which can require an interactive
        # authentication-database unlock. Save them first so canceling or failing
        # that operation cannot leave the ordinary/project settings half-updated.
        try:
            self.store.set_v1_credentials(
                self.v1_username.text(),
                self.v1_password.text(),
            )
            self.store.set_v2_api_key(self.v2_api_key.text())
        except RuntimeError as exc:
            QMessageBox.critical(self, 'Could not save settings', str(exc))
            return

        self.store.set_protocol(self.protocol.currentData())
        self.store.set_endpoint_v1(self.v1_endpoint.text())
        self.store.set_endpoint_v2(self.v2_endpoint.text())
        self.store.set_refresh_seconds(self.refresh_seconds.value())
        self.store.set_fresh_minutes(self.fresh_minutes.value())
        self.store.set_stale_minutes(self.stale_minutes.value())
        self.store.set_devices(devices)
        self.store.set_project_enabled(self.project_enabled.isChecked())
        self.store.set_project_imeis(selected_imeis)
        self.store.set_project_history_enabled(self.history_enabled.isChecked())
        self.store.set_project_history_range(
            self.history_start.date().toString('yyyy-MM-dd'),
            self.history_end.date().toString('yyyy-MM-dd'),
        )
        super().accept()
