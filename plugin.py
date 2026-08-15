# -*- coding: utf-8 -*-

from datetime import date, datetime, timedelta, timezone

from qgis.PyQt.QtCore import QTimer, QVariant
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QAction
from qgis.core import (
    QgsApplication,
    QgsCategorizedSymbolRenderer,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsLineSymbol,
    QgsMarkerSymbol,
    QgsPalLayerSettings,
    QgsProject,
    QgsProperty,
    QgsRendererCategory,
    QgsTextFormat,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
)

from .api import GarminApiError, GarminV1Client, GarminV2Client
from .dialogs import SettingsDialog
from .settings import SettingsStore, normalize_track_color
from .tasks import GarminHistoryTask, GarminRefreshTask


GROUP_NAME = 'Garmin inReach'
GROUP_PROPERTY = 'Inreach2QGIS/managed_group'
CURRENT_LAYER_NAME = 'Current location'
HISTORY_POINTS_LAYER_NAME = 'History points'
HISTORY_TRACKS_LAYER_NAME = 'History tracks'
LAYER_PROPERTY = 'Inreach2QGIS/managed'
LAYER_KIND_PROPERTY = 'Inreach2QGIS/kind'
KIND_CURRENT = 'current'
KIND_HISTORY_POINTS = 'history_points'
KIND_HISTORY_TRACKS = 'history_tracks'


class Inreach2QGISPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.store = SettingsStore()
        self.actions = []
        self.timer = QTimer()
        self.timer.timeout.connect(self.refresh)

        self.refresh_task = None
        self.refresh_generation = 0

        self.history_task = None
        self.history_generation = 0
        self.history_cache_key = None
        self.history_cache_locations = None

        self._unloading = False

    def initGui(self):
        settings_action = QAction(
            'Setup',
            self.iface.mainWindow(),
        )
        settings_action.triggered.connect(self.open_settings)
        self._add_action(settings_action)

        refresh_action = QAction(
            'Refresh',
            self.iface.mainWindow(),
        )
        refresh_action.triggered.connect(self.refresh)
        self._add_action(refresh_action)

        QgsProject.instance().readProject.connect(self._on_project_read)
        self._configure_timer()
        QTimer.singleShot(500, self.refresh_if_enabled)

    def _add_action(self, action):
        self.actions.append(action)
        self.iface.addPluginToMenu('InReach2QGIS', action)

    def unload(self):
        self._unloading = True
        self.timer.stop()
        self._invalidate_refresh()
        self._invalidate_history(clear_cache=True)
        try:
            QgsProject.instance().readProject.disconnect(self._on_project_read)
        except Exception:
            pass
        for action in self.actions:
            self.iface.removePluginMenu('InReach2QGIS', action)
        self.actions = []

    def _configure_timer(self):
        self.timer.stop()
        interval = max(15, self.store.refresh_seconds()) * 1000
        self.timer.start(interval)

    def _invalidate_refresh(self):
        self.refresh_generation += 1
        task = self.refresh_task
        self.refresh_task = None
        if task is not None and task.isActive():
            task.cancel()

    def _invalidate_history(self, clear_cache=False):
        self.history_generation += 1
        task = self.history_task
        self.history_task = None
        if task is not None and task.isActive():
            task.cancel()
        if clear_cache:
            self.history_cache_key = None
            self.history_cache_locations = None

    def _on_project_read(self, *args):
        self._invalidate_refresh()
        self._invalidate_history(clear_cache=True)
        QTimer.singleShot(300, self.refresh_if_enabled)

    def open_settings(self):
        dialog = SettingsDialog(self.store, self.iface.mainWindow())
        if dialog.exec_():
            self._invalidate_refresh()
            # Credentials live outside the project/cache key and can change in
            # this dialog, so invalidate cached history after any accepted edit.
            # The cache still avoids repeated downloads during normal refreshes.
            self._invalidate_history(clear_cache=True)
            self._configure_timer()
            self.refresh_if_enabled()

    def refresh_if_enabled(self):
        if self.store.project_enabled():
            self.refresh()
            self.refresh_history_if_enabled()
        else:
            self._remove_managed_layers()

    # ---------- clients ----------
    def _client(self):
        protocol = self.store.protocol()
        if protocol == 'v1':
            username = self.store.v1_username()
            password = self.store.v1_password()
            if not username or not password:
                raise GarminApiError('V1 login/password are not configured.')
            return GarminV1Client(self.store.endpoint_v1(), username, password)

        api_key = self.store.v2_api_key()
        if not api_key:
            raise GarminApiError('V2 API key is not configured.')
        return GarminV2Client(self.store.endpoint_v2(), api_key)

    # ---------- current positions ----------
    def refresh(self):
        if self._unloading or not self.store.project_enabled():
            return

        if self.refresh_task is not None and self.refresh_task.isActive():
            return

        selected = self.store.project_imeis()
        if not selected:
            self._remove_managed_layers()
            self.iface.messageBar().pushWarning(
                'Inreach2QGIS',
                'Tracking is enabled, but no devices are selected for this project.',
            )
            return

        try:
            client = self._client()
        except GarminApiError as exc:
            self.iface.messageBar().pushCritical('Inreach2QGIS', str(exc))
            return
        except Exception as exc:
            self.iface.messageBar().pushCritical(
                'Inreach2QGIS',
                f'Unexpected error: {exc}',
            )
            return

        task = GarminRefreshTask(
            client=client,
            imeis=selected,
            generation=self.refresh_generation,
            callback=self._refresh_finished,
        )
        self.refresh_task = task
        QgsApplication.taskManager().addTask(task)

    def _refresh_finished(self, task, result):
        if task is self.refresh_task:
            self.refresh_task = None

        if self._unloading or task.generation != self.refresh_generation:
            return

        if not result:
            if task.error:
                self.iface.messageBar().pushCritical('Inreach2QGIS', task.error)
            return

        if not self.store.project_enabled():
            return

        selected = self.store.project_imeis()
        if selected != task.imeis:
            QTimer.singleShot(0, self.refresh_if_enabled)
            return

        self._apply_locations(task.locations, selected)

    def _apply_locations(self, locations, selected):
        registry = {item['imei']: item['name'] for item in self.store.devices()}
        layer = self._get_or_create_current_layer()
        provider = layer.dataProvider()
        provider.truncate()

        now = datetime.now(timezone.utc)
        features = []
        returned = set()
        for item in locations:
            imei = str(item.get('imei', ''))
            returned.add(imei)
            timestamp_ms = item.get('timestamp_ms')
            timestamp_utc = None
            timestamp_local = None
            age_min = None
            if timestamp_ms is not None:
                timestamp_utc = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
                timestamp_local = timestamp_utc.astimezone()
                age_min = max(0.0, (now - timestamp_utc).total_seconds() / 60.0)

            status = self._status(age_min)
            feature = QgsFeature(layer.fields())
            feature.setGeometry(
                QgsGeometry.fromPointXY(
                    self._point(item['longitude'], item['latitude'])
                )
            )
            feature.setAttributes([
                registry.get(imei, imei),
                imei,
                timestamp_utc.strftime('%Y-%m-%d %H:%M:%S UTC') if timestamp_utc else '',
                timestamp_local.strftime('%Y-%m-%d %H:%M:%S %Z') if timestamp_local else '',
                round(age_min, 1) if age_min is not None else None,
                status,
                item['latitude'],
                item['longitude'],
                item.get('altitude_m'),
                item.get('speed_kmh'),
                item.get('course_deg'),
                item.get('gps_fix'),
            ])
            features.append(feature)

        provider.addFeatures(features)
        layer.updateExtents()
        layer.triggerRepaint()
        self._organize_managed_layers()

        missing = [registry.get(imei, imei) for imei in selected if imei not in returned]
        message = f'Updated {len(features)} of {len(selected)} selected device(s).'
        if missing:
            message += ' No location for: ' + ', '.join(missing)
            self.iface.messageBar().pushWarning('Inreach2QGIS', message)
        else:
            self.iface.messageBar().pushSuccess('Inreach2QGIS', message)

    # ---------- history ----------
    def _history_request(self, selected):
        start_text = self.store.project_history_start()
        end_inclusive_text = self.store.project_history_end()
        try:
            start_date = date.fromisoformat(start_text)
            end_inclusive = date.fromisoformat(end_inclusive_text)
        except ValueError as exc:
            raise GarminApiError('Invalid history date stored in the QGIS project.') from exc

        if start_date > end_inclusive:
            raise GarminApiError('History start date is after the end date.')

        # The UI presents an inclusive end date. Garmin's range behaves as an
        # end boundary, so send the following day to include the whole selected day.
        try:
            end_exclusive = end_inclusive + timedelta(days=1)
        except OverflowError as exc:
            raise GarminApiError(
                'History end date is outside the supported range.'
            ) from exc

        protocol = self.store.protocol()
        endpoint = self.store.endpoint_v1() if protocol == 'v1' else self.store.endpoint_v2()
        cache_key = (
            protocol,
            endpoint,
            tuple(selected),
            start_date.isoformat(),
            end_exclusive.isoformat(),
        )
        return cache_key, start_date.isoformat(), end_exclusive.isoformat()

    def refresh_history_if_enabled(self):
        if self._unloading:
            return

        if not self.store.project_enabled() or not self.store.project_history_enabled():
            self._remove_history_layers()
            return

        selected = self.store.project_imeis()
        if not selected:
            self._remove_history_layers()
            return

        try:
            cache_key, start_text, end_text = self._history_request(selected)
        except GarminApiError as exc:
            self._remove_history_layers()
            self.iface.messageBar().pushCritical('Inreach2QGIS history', str(exc))
            return

        if (
            self.history_cache_key == cache_key
            and self.history_cache_locations is not None
        ):
            points_exist = bool(self._managed_layers(KIND_HISTORY_POINTS))
            tracks_exist = bool(self._managed_layers(KIND_HISTORY_TRACKS))
            if not points_exist or not tracks_exist:
                self._apply_history(self.history_cache_locations, selected)
            return

        if self.history_task is not None and self.history_task.isActive():
            if self.history_task.cache_key == cache_key:
                return
            self._invalidate_history(clear_cache=False)

        # A cache miss means any visible history belongs to an older selection or
        # range. Remove it before attempting the replacement so failed requests do
        # not leave stale data masquerading as the newly requested history.
        self._remove_history_layers()

        try:
            client = self._client()
        except GarminApiError as exc:
            self.iface.messageBar().pushCritical('Inreach2QGIS history', str(exc))
            return
        except Exception as exc:
            self.iface.messageBar().pushCritical(
                'Inreach2QGIS history',
                f'Unexpected error: {exc}',
            )
            return

        task = GarminHistoryTask(
            client=client,
            imeis=selected,
            start=start_text,
            end=end_text,
            cache_key=cache_key,
            generation=self.history_generation,
            callback=self._history_finished,
        )
        self.history_task = task
        QgsApplication.taskManager().addTask(task)

    def _history_finished(self, task, result):
        if task is self.history_task:
            self.history_task = None

        if self._unloading or task.generation != self.history_generation:
            return

        if not result:
            if task.error:
                self.iface.messageBar().pushCritical(
                    'Inreach2QGIS history',
                    task.error,
                )
            return

        if not self.store.project_enabled() or not self.store.project_history_enabled():
            return

        selected = self.store.project_imeis()
        try:
            current_key, _, _ = self._history_request(selected)
        except GarminApiError:
            return
        if current_key != task.cache_key:
            QTimer.singleShot(0, self.refresh_history_if_enabled)
            return

        self.history_cache_key = task.cache_key
        self.history_cache_locations = list(task.locations)
        self._apply_history(task.locations, selected)

    def _apply_history(self, locations, selected):
        registry = {
            str(item['imei']): item
            for item in self.store.devices()
        }

        # Garmin can occasionally return exact duplicate history records. It can
        # also return a zero coordinate placeholder; do not let that create a line
        # from the Gulf of Guinea to the first real tracking point.
        unique = []
        seen = set()
        for item in locations:
            latitude = item.get('latitude')
            longitude = item.get('longitude')
            if latitude is None or longitude is None:
                continue
            if float(latitude) == 0.0 and float(longitude) == 0.0:
                continue

            key = (
                str(item.get('imei', '')),
                item.get('timestamp_ms'),
                latitude,
                longitude,
                item.get('altitude_m'),
                item.get('speed_kmh'),
                item.get('course_deg'),
                item.get('gps_fix'),
                item.get('text_message', ''),
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)

        unique.sort(key=lambda item: (
            str(item.get('imei', '')),
            item.get('timestamp_ms') if item.get('timestamp_ms') is not None else -1,
        ))

        by_imei = {}
        for item in unique:
            by_imei.setdefault(str(item.get('imei', '')), []).append(item)

        tracks_layer = self._get_or_create_history_tracks_layer()
        tracks_provider = tracks_layer.dataProvider()
        tracks_provider.truncate()
        track_features = []

        for imei in selected:
            imei = str(imei)
            rows = by_imei.get(imei, [])
            if len(rows) < 2:
                continue
            points = [
                self._point(item['longitude'], item['latitude'])
                for item in rows
            ]
            feature = QgsFeature(tracks_layer.fields())
            feature.setGeometry(QgsGeometry.fromPolylineXY(points))
            first_ms = rows[0].get('timestamp_ms')
            last_ms = rows[-1].get('timestamp_ms')
            first_utc = self._format_utc(first_ms)
            last_utc = self._format_utc(last_ms)
            device = registry.get(imei, {})
            feature.setAttributes([
                device.get('name') or imei,
                imei,
                first_utc,
                last_utc,
                len(rows),
            ])
            track_features.append(feature)

        tracks_provider.addFeatures(track_features)
        self._style_history_tracks_layer(tracks_layer, selected, registry)
        tracks_layer.updateExtents()
        tracks_layer.triggerRepaint()

        points_layer = self._get_or_create_history_points_layer()
        points_provider = points_layer.dataProvider()
        points_provider.truncate()
        point_features = []

        for item in unique:
            imei = str(item.get('imei', ''))
            timestamp_ms = item.get('timestamp_ms')
            timestamp_utc = None
            timestamp_local = None
            if timestamp_ms is not None:
                timestamp_utc = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
                timestamp_local = timestamp_utc.astimezone()

            device = registry.get(imei, {})
            feature = QgsFeature(points_layer.fields())
            feature.setGeometry(
                QgsGeometry.fromPointXY(
                    self._point(item['longitude'], item['latitude'])
                )
            )
            feature.setAttributes([
                device.get('name') or imei,
                imei,
                timestamp_utc.strftime('%Y-%m-%d %H:%M:%S UTC') if timestamp_utc else '',
                timestamp_local.strftime('%Y-%m-%d %H:%M:%S %Z') if timestamp_local else '',
                item['latitude'],
                item['longitude'],
                item.get('altitude_m'),
                item.get('speed_kmh'),
                item.get('course_deg'),
                item.get('gps_fix'),
                item.get('text_message', ''),
            ])
            point_features.append(feature)

        points_provider.addFeatures(point_features)
        self._style_history_points_layer(points_layer, selected, registry)
        points_layer.updateExtents()
        points_layer.triggerRepaint()
        self._organize_managed_layers()

        returned = {str(item.get('imei', '')) for item in unique}
        missing = []
        for imei in selected:
            imei = str(imei)
            if imei not in returned:
                device = registry.get(imei, {})
                missing.append(device.get('name') or imei)

        message = f'Loaded {len(unique)} history point(s) for {len(returned)} device(s).'
        if missing:
            message += ' No history for: ' + ', '.join(missing)
            self.iface.messageBar().pushWarning('Inreach2QGIS history', message)
        else:
            self.iface.messageBar().pushSuccess('Inreach2QGIS history', message)

    @staticmethod
    def _format_utc(timestamp_ms):
        if timestamp_ms is None:
            return ''
        value = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
        return value.strftime('%Y-%m-%d %H:%M:%S UTC')

    # ---------- geometry/status ----------
    @staticmethod
    def _point(longitude, latitude):
        from qgis.core import QgsPointXY
        return QgsPointXY(float(longitude), float(latitude))

    def _status(self, age_min):
        if age_min is None:
            return 'UNKNOWN'
        if age_min <= self.store.fresh_minutes():
            return 'FRESH'
        if age_min <= self.store.stale_minutes():
            return 'DELAYED'
        return 'STALE'

    # ---------- managed layers ----------
    @staticmethod
    def _layer_kind(layer):
        kind = str(layer.customProperty(LAYER_KIND_PROPERTY, '') or '')
        # v0.2 current-position layers did not have a kind property.
        return kind or KIND_CURRENT

    def _managed_layers(self, kind=None):
        layers = [
            layer for layer in QgsProject.instance().mapLayers().values()
            if layer.customProperty(LAYER_PROPERTY, False)
        ]
        if kind is None:
            return layers
        return [layer for layer in layers if self._layer_kind(layer) == kind]

    @staticmethod
    def _owned_group():
        root = QgsProject.instance().layerTreeRoot()
        for group in root.findGroups(True):
            if group.customProperty(GROUP_PROPERTY, False):
                return group
        return None

    def _group(self):
        root = QgsProject.instance().layerTreeRoot()
        group = self._owned_group()
        if group is None:
            group = root.insertGroup(0, GROUP_NAME)
            group.setCustomProperty(GROUP_PROPERTY, True)
            group.setExpanded(True)
        return group

    def _organize_managed_layers(self):
        group = self._group()
        root = QgsProject.instance().layerTreeRoot()
        ordered = []
        for kind, name in (
            (KIND_CURRENT, CURRENT_LAYER_NAME),
            (KIND_HISTORY_POINTS, HISTORY_POINTS_LAYER_NAME),
            (KIND_HISTORY_TRACKS, HISTORY_TRACKS_LAYER_NAME),
        ):
            layers = self._managed_layers(kind)
            if not layers:
                continue
            layer = layers[0]
            layer.setName(name)
            ordered.append(layer)

        # Move existing layer-tree nodes by cloning them before removal. The clone
        # preserves visibility and other node state instead of recreating a fresh,
        # always-visible legend node on every refresh.
        for index, layer in enumerate(ordered):
            node = root.findLayer(layer.id())
            if node is None:
                group.insertLayer(index, layer)
                continue

            parent = node.parent()
            children = list(group.children())
            if (
                parent is group
                and index < len(children)
                and children[index] == node
            ):
                continue

            clone = node.clone()
            if parent is not None:
                parent.removeChildNode(node)
            group.insertChildNode(index, clone)

    def _remove_managed_layers(self):
        for layer in self._managed_layers():
            QgsProject.instance().removeMapLayer(layer.id())
        group = self._owned_group()
        if (
            group is not None
            and not group.children()
            and group.parent() is not None
        ):
            group.parent().removeChildNode(group)

    def _remove_history_layers(self):
        for kind in (KIND_HISTORY_POINTS, KIND_HISTORY_TRACKS):
            for layer in self._managed_layers(kind):
                QgsProject.instance().removeMapLayer(layer.id())
        if self._managed_layers(KIND_CURRENT):
            self._organize_managed_layers()

    def _get_or_create_current_layer(self):
        layers = self._managed_layers(KIND_CURRENT)
        if layers:
            layer = layers[0]
            layer.setCustomProperty(LAYER_KIND_PROPERTY, KIND_CURRENT)
            layer.setName(CURRENT_LAYER_NAME)
            self._style_current_layer(layer)
            return layer

        layer = QgsVectorLayer('Point?crs=EPSG:4326', CURRENT_LAYER_NAME, 'memory')
        layer.setCustomProperty(LAYER_PROPERTY, True)
        layer.setCustomProperty(LAYER_KIND_PROPERTY, KIND_CURRENT)
        provider = layer.dataProvider()
        provider.addAttributes([
            QgsField('name', QVariant.String),
            QgsField('imei', QVariant.String),
            QgsField('time_utc', QVariant.String),
            QgsField('time_local', QVariant.String),
            QgsField('age_min', QVariant.Double),
            QgsField('status', QVariant.String),
            QgsField('latitude', QVariant.Double),
            QgsField('longitude', QVariant.Double),
            QgsField('altitude_m', QVariant.Double),
            QgsField('speed_kmh', QVariant.Double),
            QgsField('course_deg', QVariant.Double),
            QgsField('gps_fix', QVariant.Int),
        ])
        layer.updateFields()
        self._style_current_layer(layer)
        QgsProject.instance().addMapLayer(layer)
        return layer

    def _get_or_create_history_points_layer(self):
        layers = self._managed_layers(KIND_HISTORY_POINTS)
        if layers:
            layer = layers[0]
            layer.setName(HISTORY_POINTS_LAYER_NAME)
            return layer

        layer = QgsVectorLayer(
            'Point?crs=EPSG:4326',
            HISTORY_POINTS_LAYER_NAME,
            'memory',
        )
        layer.setCustomProperty(LAYER_PROPERTY, True)
        layer.setCustomProperty(LAYER_KIND_PROPERTY, KIND_HISTORY_POINTS)
        provider = layer.dataProvider()
        provider.addAttributes([
            QgsField('name', QVariant.String),
            QgsField('imei', QVariant.String),
            QgsField('time_utc', QVariant.String),
            QgsField('time_local', QVariant.String),
            QgsField('latitude', QVariant.Double),
            QgsField('longitude', QVariant.Double),
            QgsField('altitude_m', QVariant.Double),
            QgsField('speed_kmh', QVariant.Double),
            QgsField('course_deg', QVariant.Double),
            QgsField('gps_fix', QVariant.Int),
            QgsField('message', QVariant.String),
        ])
        layer.updateFields()
        symbol = QgsMarkerSymbol.createSimple({
            'name': 'triangle',
            'color': '#2563eb',
            'outline_color': '#ffffff',
            'outline_width': '0.15',
            'size': '2',
        })
        symbol.setDataDefinedAngle(QgsProperty.fromField('course_deg'))
        layer.renderer().setSymbol(symbol)
        QgsProject.instance().addMapLayer(layer)
        return layer

    def _get_or_create_history_tracks_layer(self):
        layers = self._managed_layers(KIND_HISTORY_TRACKS)
        if layers:
            layer = layers[0]
            layer.setName(HISTORY_TRACKS_LAYER_NAME)
            return layer

        layer = QgsVectorLayer(
            'LineString?crs=EPSG:4326',
            HISTORY_TRACKS_LAYER_NAME,
            'memory',
        )
        layer.setCustomProperty(LAYER_PROPERTY, True)
        layer.setCustomProperty(LAYER_KIND_PROPERTY, KIND_HISTORY_TRACKS)
        provider = layer.dataProvider()
        provider.addAttributes([
            QgsField('name', QVariant.String),
            QgsField('imei', QVariant.String),
            QgsField('start_utc', QVariant.String),
            QgsField('end_utc', QVariant.String),
            QgsField('point_count', QVariant.Int),
        ])
        layer.updateFields()
        symbol = QgsLineSymbol.createSimple({
            'color': '#2563eb',
            'width': '0.8',
        })
        layer.renderer().setSymbol(symbol)
        QgsProject.instance().addMapLayer(layer)
        return layer

    @staticmethod
    def _style_history_tracks_layer(layer, selected, registry):
        categories = []
        for imei in selected:
            imei = str(imei)
            device = registry.get(imei, {})
            color = normalize_track_color(device.get('color', ''), imei)
            name = device.get('name') or imei
            symbol = QgsLineSymbol.createSimple({
                'color': color,
                'width': '0.8',
            })
            categories.append(QgsRendererCategory(imei, symbol, name))
        layer.setRenderer(QgsCategorizedSymbolRenderer('imei', categories))

    @staticmethod
    def _style_history_points_layer(layer, selected, registry):
        categories = []
        for imei in selected:
            imei = str(imei)
            device = registry.get(imei, {})
            color = normalize_track_color(device.get('color', ''), imei)
            name = device.get('name') or imei
            symbol = QgsMarkerSymbol.createSimple({
                'name': 'triangle',
                'color': color,
                'outline_color': '#ffffff',
                'outline_width': '0.15',
                'size': '2',
            })
            symbol.setDataDefinedAngle(QgsProperty.fromField('course_deg'))
            categories.append(QgsRendererCategory(imei, symbol, name))
        layer.setRenderer(QgsCategorizedSymbolRenderer('imei', categories))

    @staticmethod
    def _style_current_layer(layer):
        categories = []
        for value, color in (
            ('FRESH', '#2ca25f'),
            ('DELAYED', '#f0ad4e'),
            ('STALE', '#d9534f'),
            ('UNKNOWN', '#777777'),
        ):
            symbol = QgsMarkerSymbol.createSimple({
                'name': 'arrow',
                'color': color,
                'outline_color': '#333333',
                'outline_width': '0.4',
                'size': '6',
            })
            symbol.setDataDefinedAngle(QgsProperty.fromField('course_deg'))
            categories.append(QgsRendererCategory(value, symbol, value.title()))
        layer.setRenderer(QgsCategorizedSymbolRenderer('status', categories))

        label = QgsPalLayerSettings()
        label.enabled = True
        label.fieldName = (
            "CASE WHEN age_min IS NULL THEN name || ' | unknown' "
            "WHEN age_min < 60 THEN name || ' | ' || round(age_min,1) || ' min ago' "
            "ELSE name || ' | ' || round(age_min/60,1) || ' h ago' END"
        )
        label.isExpression = True
        text_format = QgsTextFormat()
        text_format.setSize(9)
        text_format.setColor(QColor('#222222'))
        label.setFormat(text_format)
        layer.setLabeling(QgsVectorLayerSimpleLabeling(label))
        layer.setLabelsEnabled(True)
