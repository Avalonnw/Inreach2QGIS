# -*- coding: utf-8 -*-

import os

from qgis.PyQt.QtCore import QTimer
from qgis.core import (
    QgsApplication,
    QgsFeature,
    QgsGeometry,
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

from .api import GarminApiError
from .plugin import (
    CURRENT_LAYER_NAME,
    HISTORY_POINTS_LAYER_NAME,
    HISTORY_TRACKS_LAYER_NAME,
    KIND_CURRENT,
    KIND_HISTORY_POINTS,
    KIND_HISTORY_TRACKS,
    LAYER_KIND_PROPERTY,
    LAYER_PROPERTY,
    Inreach2QGISPlugin,
)
from .tasks import GarminHistoryTask


TABLE_NAMES = {
    KIND_CURRENT: 'current_location',
    KIND_HISTORY_POINTS: 'history_points',
    KIND_HISTORY_TRACKS: 'history_tracks',
}

DISPLAY_NAMES = {
    KIND_CURRENT: CURRENT_LAYER_NAME,
    KIND_HISTORY_POINTS: HISTORY_POINTS_LAYER_NAME,
    KIND_HISTORY_TRACKS: HISTORY_TRACKS_LAYER_NAME,
}


class PersistentInreach2QGISPlugin(Inreach2QGISPlugin):
    """Inreach2QGIS with project-local persistent GeoPackage storage."""

    def __init__(self, iface):
        super().__init__(iface)
        self._storage_warning_shown = False
        self._storage_error_text = ''

    # ---------- project storage ----------
    @staticmethod
    def _storage_path():
        project_file = str(QgsProject.instance().fileName() or '').strip()
        if not project_file:
            return ''
        base, _ = os.path.splitext(os.path.abspath(project_file))
        return base + '_inreach.gpkg'

    @staticmethod
    def _normalized_path(path):
        text = str(path or '').strip()
        if not text:
            return ''
        return os.path.normcase(os.path.abspath(text))

    @classmethod
    def _layer_storage_path(cls, layer):
        if layer is None or layer.providerType() != 'ogr':
            return ''
        source = str(layer.source() or '')
        path = source.split('|', 1)[0]
        return cls._normalized_path(path)

    def _warn_if_storage_is_temporary(self):
        if self._storage_path() or self._storage_warning_shown:
            return
        self._storage_warning_shown = True
        self.iface.messageBar().pushWarning(
            'Inreach2QGIS storage',
            'Save this QGIS project to enable persistent Garmin storage. '
            'Until the project has a filename, Garmin layers are temporary.',
        )

    def _storage_error(self, message):
        text = str(message or 'Unknown persistent storage error.')
        if text == self._storage_error_text:
            return
        self._storage_error_text = text
        self.iface.messageBar().pushCritical('Inreach2QGIS storage', text)

    @staticmethod
    def _table_exists(path, table_name):
        if not path or not os.path.exists(path):
            return False
        try:
            return bool(QgsVectorFileWriter.targetLayerExists(path, table_name))
        except Exception:
            return False

    @staticmethod
    def _write_memory_layer(layer, path, table_name):
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = 'GPKG'
        options.fileEncoding = 'UTF-8'
        options.layerName = table_name
        options.actionOnExistingFile = (
            QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
            if os.path.exists(path)
            else QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile
        )
        result = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer,
            path,
            QgsProject.instance().transformContext(),
            options,
        )
        code = result[0] if isinstance(result, (tuple, list)) else result
        if code != QgsVectorFileWriter.WriterError.NoError:
            detail = ''
            if isinstance(result, (tuple, list)) and len(result) > 1:
                detail = str(result[1] or '')
            raise RuntimeError(
                f'Could not create Garmin GeoPackage layer {table_name}: '
                f'{detail or code}'
            )

    @staticmethod
    def _provider_add_features(provider, features):
        if not features:
            return True, 0
        result = provider.addFeatures(features)
        if isinstance(result, (tuple, list)):
            ok = bool(result[0]) if result else False
            written = result[1] if len(result) > 1 else None
        else:
            ok = bool(result)
            written = None
        if not ok:
            return False, 0
        if written is not None:
            try:
                count = len(written)
            except TypeError:
                count = len(features)
            if count != len(features):
                return False, count
        return True, len(features)

    def _load_storage_layer(self, kind):
        path = self._storage_path()
        table_name = TABLE_NAMES[kind]
        if not self._table_exists(path, table_name):
            return None

        layer = QgsVectorLayer(
            f'{path}|layername={table_name}',
            DISPLAY_NAMES[kind],
            'ogr',
        )
        if not layer.isValid():
            self._storage_error(
                f'Could not open persistent Garmin layer {table_name} in {path}.'
            )
            return None

        layer.setCustomProperty(LAYER_PROPERTY, True)
        layer.setCustomProperty(LAYER_KIND_PROPERTY, kind)
        QgsProject.instance().addMapLayer(layer, False)
        self._style_layer_for_kind(layer, kind)
        self._organize_managed_layers()
        return layer

    def _style_layer_for_kind(self, layer, kind):
        if kind == KIND_CURRENT:
            self._style_current_layer(layer)
            return

        registry = {
            str(item['imei']): item
            for item in self.store.devices()
        }
        selected = self.store.project_imeis()
        if kind == KIND_HISTORY_POINTS:
            self._style_history_points_layer(layer, selected, registry)
        elif kind == KIND_HISTORY_TRACKS:
            self._style_history_tracks_layer(layer, selected, registry)

    def _persist_memory_layer(self, layer, kind):
        if layer is None:
            return layer

        path = self._storage_path()
        if not path:
            return layer

        target_path = self._normalized_path(path)
        provider_type = layer.providerType()
        if provider_type == 'ogr' and self._layer_storage_path(layer) == target_path:
            return layer
        if provider_type not in ('memory', 'ogr'):
            return layer

        table_name = TABLE_NAMES[kind]
        old_node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
        old_visible = old_node.itemVisibilityChecked() if old_node is not None else True
        old_expanded = old_node.isExpanded() if old_node is not None else True

        old_subset = ''
        subset_cleared = False
        try:
            # Save As can leave managed OGR layers bound to the old project's
            # GeoPackage. Copy the complete source layer to the new project-local
            # GeoPackage, not merely the currently displayed History subset.
            if provider_type == 'ogr':
                old_subset = str(layer.subsetString() or '')
                if old_subset:
                    if not layer.setSubsetString(''):
                        raise RuntimeError(
                            f'Could not clear the {DISPLAY_NAMES[kind]} filter before '
                            'rebinding persistent Garmin storage.'
                        )
                    subset_cleared = True
                self._write_memory_layer(layer, path, table_name)
            elif not self._table_exists(path, table_name):
                self._write_memory_layer(layer, path, table_name)

            stored = QgsVectorLayer(
                f'{path}|layername={table_name}',
                DISPLAY_NAMES[kind],
                'ogr',
            )
            if not stored.isValid():
                raise RuntimeError(
                    f'Could not reopen Garmin GeoPackage layer {table_name} in {path}.'
                )
        except Exception as exc:
            if subset_cleared:
                layer.setSubsetString(old_subset)
            self._storage_error(exc)
            return layer

        if subset_cleared:
            layer.setSubsetString(old_subset)

        stored.setCustomProperty(LAYER_PROPERTY, True)
        stored.setCustomProperty(LAYER_KIND_PROPERTY, kind)
        self._style_layer_for_kind(stored, kind)

        project = QgsProject.instance()
        project.addMapLayer(stored, False)
        project.removeMapLayer(layer.id())
        self._organize_managed_layers()

        new_node = project.layerTreeRoot().findLayer(stored.id())
        if new_node is not None:
            new_node.setItemVisibilityChecked(old_visible)
            new_node.setExpanded(old_expanded)

        self._storage_error_text = ''
        return stored

    def _persist_all_managed_layers(self):
        if not self._storage_path():
            return
        for kind in (KIND_CURRENT, KIND_HISTORY_POINTS, KIND_HISTORY_TRACKS):
            layers = self._managed_layers(kind)
            if layers:
                self._persist_memory_layer(layers[0], kind)

    # ---------- current positions ----------
    def refresh(self):
        if not self._unloading and self.store.project_enabled():
            self._warn_if_storage_is_temporary()
            self._persist_all_managed_layers()
            if not self._managed_layers(KIND_CURRENT):
                self._load_storage_layer(KIND_CURRENT)
        super().refresh()

    def _get_or_create_current_layer(self):
        layers = self._managed_layers(KIND_CURRENT)
        if layers:
            layer = self._persist_memory_layer(layers[0], KIND_CURRENT)
            layer.setCustomProperty(LAYER_KIND_PROPERTY, KIND_CURRENT)
            layer.setName(CURRENT_LAYER_NAME)
            self._style_current_layer(layer)
            return layer

        stored = self._load_storage_layer(KIND_CURRENT)
        if stored is not None:
            return stored

        layer = super()._get_or_create_current_layer()
        return self._persist_memory_layer(layer, KIND_CURRENT)

    def _apply_locations(self, locations, selected):
        # Current position is mutable state, but once the project is saved it is
        # also part of the persistent project archive. Do not report a successful
        # refresh unless the backing layer was actually cleared and rewritten.
        from datetime import datetime, timezone

        registry = {item['imei']: item['name'] for item in self.store.devices()}
        layer = self._get_or_create_current_layer()
        provider = layer.dataProvider()

        storage_path = self._storage_path()
        persistent_ok = (
            bool(storage_path)
            and layer.providerType() == 'ogr'
            and self._layer_storage_path(layer) == self._normalized_path(storage_path)
        )

        if not self._clear_layer(layer):
            self._storage_error(
                'Could not clear the Garmin Current location layer before refresh. '
                'The previous positions were left in place.'
            )
            return False

        # Verify the clear independently of the provider return value. This matters
        # for locked/read-only/network GeoPackages where an operation may report an
        # incomplete result.
        if any(layer.getFeatures()):
            self._storage_error(
                'Garmin Current location archive could not be cleared completely. '
                'The refresh was not accepted.'
            )
            return False

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
                timestamp_utc = datetime.fromtimestamp(
                    timestamp_ms / 1000.0,
                    tz=timezone.utc,
                )
                timestamp_local = timestamp_utc.astimezone()
                age_min = max(
                    0.0,
                    (now - timestamp_utc).total_seconds() / 60.0,
                )

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

        ok, written = self._provider_add_features(provider, features)
        if not ok:
            self._storage_error(
                f'Could not write Garmin Current location: wrote {written} of '
                f'{len(features)} feature(s). The next refresh will retry.'
            )
            return False

        stored_count = sum(1 for _ in layer.getFeatures())
        if stored_count != len(features):
            self._storage_error(
                f'Garmin Current location verification failed: expected '
                f'{len(features)} feature(s), found {stored_count}. '
                'The next refresh will retry.'
            )
            return False

        layer.updateExtents()
        layer.triggerRepaint()
        self._organize_managed_layers()

        missing = [
            registry.get(imei, imei)
            for imei in selected
            if imei not in returned
        ]
        message = f'Updated {len(features)} of {len(selected)} selected device(s).'
        if storage_path and not persistent_ok:
            message += ' Persistent storage is unavailable; this update is temporary.'
            if missing:
                message += ' No location for: ' + ', '.join(missing)
            self.iface.messageBar().pushWarning('Inreach2QGIS', message)
            return True

        self._storage_error_text = ''
        if missing:
            message += ' No location for: ' + ', '.join(missing)
            self.iface.messageBar().pushWarning('Inreach2QGIS', message)
        else:
            self.iface.messageBar().pushSuccess('Inreach2QGIS', message)
        return True

    # ---------- persistent history ----------
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

        self._warn_if_storage_is_temporary()
        self._persist_all_managed_layers()

        try:
            cache_key, start_text, end_text = self._history_request(selected)
        except GarminApiError as exc:
            self.iface.messageBar().pushCritical('Inreach2QGIS history', str(exc))
            return

        # Load the local archive before attempting the Garmin request. This keeps
        # previously saved history visible even if Garmin is offline or old data
        # has disappeared upstream.
        points_layer = self._get_or_create_history_points_layer()
        tracks_layer = self._get_or_create_history_tracks_layer()
        self._apply_history_subset(points_layer, selected, start_text, end_text)
        self._rebuild_tracks_from_local(points_layer, tracks_layer, selected, start_text, end_text)
        self._organize_managed_layers()

        if (
            self.history_cache_key == cache_key
            and self.history_cache_locations is not None
        ):
            return

        if self.history_task is not None and self.history_task.isActive():
            if self.history_task.cache_key == cache_key:
                return
            self._invalidate_history(clear_cache=False)

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

        # Do not cache a successful network response until the local archive has
        # actually accepted it. A locked/read-only/full GeoPackage must be retried.
        if not self._apply_history(task.locations, selected):
            self.history_cache_key = None
            self.history_cache_locations = None
            return

        self.history_cache_key = task.cache_key
        self.history_cache_locations = list(task.locations)

    def _get_or_create_history_points_layer(self):
        layers = self._managed_layers(KIND_HISTORY_POINTS)
        if layers:
            layer = self._persist_memory_layer(layers[0], KIND_HISTORY_POINTS)
            layer.setName(HISTORY_POINTS_LAYER_NAME)
            return layer

        stored = self._load_storage_layer(KIND_HISTORY_POINTS)
        if stored is not None:
            return stored

        layer = super()._get_or_create_history_points_layer()
        return self._persist_memory_layer(layer, KIND_HISTORY_POINTS)

    def _get_or_create_history_tracks_layer(self):
        layers = self._managed_layers(KIND_HISTORY_TRACKS)
        if layers:
            layer = self._persist_memory_layer(layers[0], KIND_HISTORY_TRACKS)
            layer.setName(HISTORY_TRACKS_LAYER_NAME)
            return layer

        stored = self._load_storage_layer(KIND_HISTORY_TRACKS)
        if stored is not None:
            return stored

        layer = super()._get_or_create_history_tracks_layer()
        return self._persist_memory_layer(layer, KIND_HISTORY_TRACKS)

    @staticmethod
    def _history_value(value, numeric=False, integer=False):
        if value is None or value == '':
            return None
        if integer:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
        if numeric:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
        return str(value)

    @classmethod
    def _history_record_key(
        cls,
        imei,
        time_utc,
        latitude,
        longitude,
        altitude_m,
        speed_kmh,
        course_deg,
        gps_fix,
        message,
    ):
        return (
            str(imei or ''),
            str(time_utc or ''),
            cls._history_value(latitude, numeric=True),
            cls._history_value(longitude, numeric=True),
            cls._history_value(altitude_m, numeric=True),
            cls._history_value(speed_kmh, numeric=True),
            cls._history_value(course_deg, numeric=True),
            cls._history_value(gps_fix, integer=True),
            str(message or ''),
        )

    @staticmethod
    def _history_time_bounds(start_text, end_text):
        return (
            f'{start_text} 00:00:00 UTC',
            f'{end_text} 00:00:00 UTC',
        )

    def _history_row_in_range(self, feature, selected, start_text, end_text):
        imei = str(feature['imei'] or '')
        if imei not in {str(value) for value in selected}:
            return False
        start_utc, end_utc = self._history_time_bounds(start_text, end_text)
        time_utc = str(feature['time_utc'] or '')
        return bool(time_utc and start_utc <= time_utc < end_utc)

    def _apply_history_subset(self, layer, selected, start_text, end_text):
        if layer.providerType() == 'memory':
            return
        escaped = [str(value).replace("'", "''") for value in selected]
        imeis = ','.join(f"'{value}'" for value in escaped)
        start_utc, end_utc = self._history_time_bounds(start_text, end_text)
        subset = (
            f'"imei" IN ({imeis}) AND '
            f'"time_utc" >= \'{start_utc}\' AND '
            f'"time_utc" < \'{end_utc}\''
        )
        if not layer.setSubsetString(subset):
            self._storage_error(
                'Could not apply the Garmin history date/device filter to the local archive.'
            )

    @staticmethod
    def _clear_layer(layer):
        provider = layer.dataProvider()
        if provider.truncate():
            return True
        ids = [feature.id() for feature in layer.getFeatures()]
        return not ids or bool(provider.deleteFeatures(ids))

    def _rebuild_tracks_from_local(
        self,
        points_layer,
        tracks_layer,
        selected,
        start_text,
        end_text,
    ):
        selected_text = {str(value) for value in selected}
        by_imei = {}
        for feature in points_layer.getFeatures():
            imei = str(feature['imei'] or '')
            if imei not in selected_text:
                continue
            if not self._history_row_in_range(feature, selected, start_text, end_text):
                continue
            by_imei.setdefault(imei, []).append(feature)

        for rows in by_imei.values():
            rows.sort(key=lambda feature: str(feature['time_utc'] or ''))

        if not self._clear_layer(tracks_layer):
            self._storage_error('Could not clear the derived Garmin History tracks layer.')
            return False

        registry = {
            str(item['imei']): item
            for item in self.store.devices()
        }
        track_features = []
        for imei in selected:
            imei = str(imei)
            rows = by_imei.get(imei, [])
            if len(rows) < 2:
                continue

            points = [
                self._point(feature['longitude'], feature['latitude'])
                for feature in rows
            ]
            feature = QgsFeature(tracks_layer.fields())
            feature.setGeometry(QgsGeometry.fromPolylineXY(points))
            device = registry.get(imei, {})
            feature.setAttributes([
                device.get('name') or imei,
                imei,
                str(rows[0]['time_utc'] or ''),
                str(rows[-1]['time_utc'] or ''),
                len(rows),
            ])
            track_features.append(feature)

        ok, written = self._provider_add_features(
            tracks_layer.dataProvider(),
            track_features,
        )
        if not ok:
            self._storage_error(
                f'Could not rebuild Garmin History tracks: wrote {written} of '
                f'{len(track_features)} feature(s).'
            )
            return False

        self._style_history_tracks_layer(tracks_layer, selected, registry)
        tracks_layer.updateExtents()
        tracks_layer.triggerRepaint()
        return True

    def _apply_history(self, locations, selected):
        registry = {
            str(item['imei']): item
            for item in self.store.devices()
        }
        try:
            _, start_text, end_text = self._history_request(selected)
        except GarminApiError:
            return False

        points_layer = self._get_or_create_history_points_layer()
        # Deduplication must see the full local archive, not only the current
        # display subset.
        if points_layer.providerType() != 'memory':
            if not points_layer.setSubsetString(''):
                self._storage_error(
                    'Could not clear the Garmin History filter before archive update.'
                )
                return False

        existing = set()
        for feature in points_layer.getFeatures():
            existing.add(self._history_record_key(
                feature['imei'],
                feature['time_utc'],
                feature['latitude'],
                feature['longitude'],
                feature['altitude_m'],
                feature['speed_kmh'],
                feature['course_deg'],
                feature['gps_fix'],
                feature['message'],
            ))

        new_features = []
        new_keys = []
        seen_remote = set()
        for item in locations:
            latitude = item.get('latitude')
            longitude = item.get('longitude')
            if latitude is None or longitude is None:
                continue
            if float(latitude) == 0.0 and float(longitude) == 0.0:
                continue

            imei = str(item.get('imei', ''))
            timestamp_ms = item.get('timestamp_ms')
            time_utc = self._format_utc(timestamp_ms)
            key = self._history_record_key(
                imei,
                time_utc,
                latitude,
                longitude,
                item.get('altitude_m'),
                item.get('speed_kmh'),
                item.get('course_deg'),
                item.get('gps_fix'),
                item.get('text_message', ''),
            )
            if key in seen_remote:
                continue
            seen_remote.add(key)
            if key in existing:
                continue
            existing.add(key)

            timestamp_local = None
            if timestamp_ms is not None:
                from datetime import datetime, timezone
                timestamp_utc = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
                timestamp_local = timestamp_utc.astimezone()

            device = registry.get(imei, {})
            feature = QgsFeature(points_layer.fields())
            feature.setGeometry(
                QgsGeometry.fromPointXY(
                    self._point(longitude, latitude)
                )
            )
            feature.setAttributes([
                device.get('name') or imei,
                imei,
                time_utc,
                timestamp_local.strftime('%Y-%m-%d %H:%M:%S %Z') if timestamp_local else '',
                latitude,
                longitude,
                item.get('altitude_m'),
                item.get('speed_kmh'),
                item.get('course_deg'),
                item.get('gps_fix'),
                item.get('text_message', ''),
            ])
            new_features.append(feature)
            new_keys.append(key)

        if new_features:
            ok, written = self._provider_add_features(
                points_layer.dataProvider(),
                new_features,
            )
            if not ok:
                self._storage_error(
                    f'Could not archive Garmin History points: wrote {written} of '
                    f'{len(new_features)} new point(s). The request will be retried.'
                )
                self._apply_history_subset(points_layer, selected, start_text, end_text)
                return False

            # Verify the actual archive contents too. This catches a provider that
            # reports success after only a partial/failed network-filesystem write.
            stored_keys = {
                self._history_record_key(
                    feature['imei'],
                    feature['time_utc'],
                    feature['latitude'],
                    feature['longitude'],
                    feature['altitude_m'],
                    feature['speed_kmh'],
                    feature['course_deg'],
                    feature['gps_fix'],
                    feature['message'],
                )
                for feature in points_layer.getFeatures()
            }
            missing_keys = [key for key in new_keys if key not in stored_keys]
            if missing_keys:
                self._storage_error(
                    f'Garmin History archive verification failed for '
                    f'{len(missing_keys)} of {len(new_keys)} new point(s). '
                    'The request will be retried.'
                )
                self._apply_history_subset(points_layer, selected, start_text, end_text)
                return False

        self._storage_error_text = ''
        self._style_history_points_layer(points_layer, selected, registry)
        points_layer.updateExtents()
        points_layer.triggerRepaint()

        self._apply_history_subset(points_layer, selected, start_text, end_text)
        tracks_layer = self._get_or_create_history_tracks_layer()
        tracks_ok = self._rebuild_tracks_from_local(
            points_layer,
            tracks_layer,
            selected,
            start_text,
            end_text,
        )
        self._organize_managed_layers()
        if not tracks_ok:
            return False

        stored_in_range = sum(
            1
            for feature in points_layer.getFeatures()
            if self._history_row_in_range(feature, selected, start_text, end_text)
        )
        local_imeis = {
            str(feature['imei'] or '')
            for feature in points_layer.getFeatures()
            if self._history_row_in_range(feature, selected, start_text, end_text)
        }
        missing = []
        for imei in selected:
            imei = str(imei)
            if imei not in local_imeis:
                device = registry.get(imei, {})
                missing.append(device.get('name') or imei)

        message = (
            f'History archive: added {len(new_features)} new point(s); '
            f'{stored_in_range} stored in the selected range.'
        )
        if missing:
            message += ' No history for: ' + ', '.join(missing)
            self.iface.messageBar().pushWarning('Inreach2QGIS history', message)
        else:
            self.iface.messageBar().pushSuccess('Inreach2QGIS history', message)
        return True
