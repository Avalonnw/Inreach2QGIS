# -*- coding: utf-8 -*-

from datetime import datetime, timezone

from qgis.PyQt.QtCore import QTimer
from qgis.core import QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer

from .api import GarminApiError
from .plugin import (
    CURRENT_LAYER_NAME,
    HISTORY_POINTS_LAYER_NAME,
    HISTORY_TRACKS_LAYER_NAME,
    KIND_CURRENT,
    KIND_HISTORY_POINTS,
    KIND_HISTORY_TRACKS,
)
from .persistent_plugin import TABLE_NAMES
from .persistent_safety import SafePersistentInreach2QGISPlugin


class RobustInreach2QGISPlugin(SafePersistentInreach2QGISPlugin):
    """Runtime hardening for QGIS layer-tree and GeoPackage edge cases."""

    @staticmethod
    def _set_attributes_by_name(feature, values):
        """Populate a feature without assuming provider field order."""
        fields = feature.fields()
        missing = []
        for name, value in values.items():
            index = fields.indexFromName(name)
            if index < 0:
                missing.append(name)
                continue
            feature.setAttribute(index, value)
        if missing:
            raise RuntimeError(
                'Garmin layer schema is missing field(s): ' + ', '.join(missing)
            )

    def _organize_managed_layers(self):
        """Keep managed layer-tree nodes stable and move them without deleting them."""
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

        wanted_ids = [layer.id() for layer in ordered]
        if not wanted_ids:
            return

        # Fast path uses only concrete layer IDs and direct child positions. Avoid
        # relying on Python wrapper identity for parent() objects.
        all_nodes = list(root.findLayers())
        node_counts = {
            layer_id: sum(1 for node in all_nodes if node.layerId() == layer_id)
            for layer_id in wanted_ids
        }
        children = list(group.children())
        leading_children = children[:len(wanted_ids)]
        healthy = (
            all(node_counts[layer_id] == 1 for layer_id in wanted_ids)
            and len(leading_children) == len(wanted_ids)
            and all(
                hasattr(child, 'layerId') and child.layerId() == layer_id
                for child, layer_id in zip(leading_children, wanted_ids)
            )
        )
        if healthy:
            return

        # Recovery path: keep one existing node for each managed map layer and
        # MOVE that same node with takeChild(). QGIS removeChildNode() deletes the
        # node, which is precisely what we must avoid here. Only true duplicate
        # presentation nodes are deleted.
        for index, layer in enumerate(ordered):
            layer_id = layer.id()
            nodes = [
                node for node in list(root.findLayers())
                if node.layerId() == layer_id
            ]

            if not nodes:
                group.insertLayer(index, layer)
                continue

            node = nodes[0]
            for duplicate in nodes[1:]:
                parent = duplicate.parent()
                if parent is not None:
                    parent.removeChildNode(duplicate)

            parent = node.parent()
            if parent is not None:
                if not parent.takeChild(node):
                    # Leave the existing presentation untouched rather than
                    # deleting/recreating it if QGIS refuses to detach it safely.
                    continue

            group.insertChildNode(index, node)

    def _persist_memory_layer(self, layer, kind):
        """Preserve a fresh temporary Current snapshot when storage already exists."""
        if layer is None:
            return layer

        path = self._storage_path()
        if (
            path
            and layer.providerType() == 'memory'
            and kind == KIND_CURRENT
            and self._table_exists(path, TABLE_NAMES[KIND_CURRENT])
        ):
            # Current location is mutable state, unlike the append-only History
            # archive. A fresh memory snapshot must replace a stale existing table
            # before the persistent layer is adopted.
            try:
                self._write_memory_layer(layer, path, TABLE_NAMES[KIND_CURRENT])
            except Exception as exc:
                self._storage_error(exc)
                return layer

        return super()._persist_memory_layer(layer, kind)

    def _apply_locations(self, locations, selected):
        """Write Current location by field name, not provider field position."""
        registry = {
            str(item['imei']): item['name']
            for item in self.store.devices()
        }
        layer = self._get_or_create_current_layer()
        if self._is_foreign_persistent_layer(layer):
            self._reject_foreign_archive('Current location')
            return False

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

        if any(layer.getFeatures()):
            self._storage_error(
                'Garmin Current location archive could not be cleared completely. '
                'The refresh was not accepted.'
            )
            return False

        now = datetime.now(timezone.utc)
        features = []
        returned = set()

        try:
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

                latitude = float(item['latitude'])
                longitude = float(item['longitude'])
                altitude = self._history_value(item.get('altitude_m'), numeric=True)
                speed = self._history_value(item.get('speed_kmh'), numeric=True)
                course = self._history_value(item.get('course_deg'), numeric=True)
                gps_fix = self._history_value(item.get('gps_fix'), integer=True)

                feature = QgsFeature(layer.fields())
                feature.setGeometry(
                    QgsGeometry.fromPointXY(self._point(longitude, latitude))
                )
                self._set_attributes_by_name(feature, {
                    'name': registry.get(imei, imei),
                    'imei': imei,
                    'time_utc': (
                        timestamp_utc.strftime('%Y-%m-%d %H:%M:%S UTC')
                        if timestamp_utc else ''
                    ),
                    'time_local': (
                        timestamp_local.strftime('%Y-%m-%d %H:%M:%S %Z')
                        if timestamp_local else ''
                    ),
                    'age_min': round(age_min, 1) if age_min is not None else None,
                    'status': self._status(age_min),
                    'latitude': latitude,
                    'longitude': longitude,
                    'altitude_m': altitude,
                    'speed_kmh': speed,
                    'course_deg': course,
                    'gps_fix': gps_fix,
                })
                features.append(feature)
        except Exception as exc:
            self._storage_error(f'Could not build Garmin Current location feature: {exc}')
            return False

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
            registry.get(str(imei), str(imei))
            for imei in selected
            if str(imei) not in returned
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

    def _merge_memory_history_into_existing(self, source_layer, path):
        """Merge History into an existing table without positional field copying."""
        table_name = TABLE_NAMES[KIND_HISTORY_POINTS]
        stored = QgsVectorLayer(
            f'{path}|layername={table_name}',
            HISTORY_POINTS_LAYER_NAME,
            'ogr',
        )
        if not stored.isValid():
            raise RuntimeError(
                f'Could not open existing Garmin History archive {table_name} in {path}.'
            )

        existing = {
            self._history_feature_key(feature)
            for feature in stored.getFeatures()
        }
        additions = []
        added_keys = []
        target_names = [field.name() for field in stored.fields()]

        for source in source_layer.getFeatures():
            key = self._history_feature_key(source)
            if key in existing:
                continue
            existing.add(key)

            feature = QgsFeature(stored.fields())
            feature.setGeometry(source.geometry())
            values = {}
            source_fields = source.fields()
            for name in target_names:
                source_index = source_fields.indexFromName(name)
                if source_index >= 0:
                    values[name] = source.attribute(source_index)
            self._set_attributes_by_name(feature, values)
            additions.append(feature)
            added_keys.append(key)

        if not additions:
            return

        ok, written = self._provider_add_features(stored.dataProvider(), additions)
        if not ok:
            raise RuntimeError(
                f'Could not merge Garmin History into the existing archive: '
                f'wrote {written} of {len(additions)} point(s).'
            )

        stored_keys = {
            self._history_feature_key(feature)
            for feature in stored.getFeatures()
        }
        missing = [key for key in added_keys if key not in stored_keys]
        if missing:
            raise RuntimeError(
                f'Garmin History merge verification failed for {len(missing)} of '
                f'{len(added_keys)} point(s).'
            )

    def _history_finished(self, task, result):
        """Apply partial History data but never cache failed IMEIs as complete."""
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

        if not self._apply_history(task.locations, selected):
            self.history_cache_key = None
            self.history_cache_locations = None
            return

        failures = list(getattr(task, 'failures', []) or [])
        if failures:
            self.history_cache_key = None
            self.history_cache_locations = None
            registry = {
                str(item['imei']): item['name']
                for item in self.store.devices()
            }
            details = '; '.join(
                f'{registry.get(imei, imei)} ({imei}): {error}'
                for imei, error in failures
            )
            self.iface.messageBar().pushWarning(
                'Inreach2QGIS history',
                'History was loaded for the devices that succeeded, but failed '
                f'device request(s) remain retryable: {details}',
            )
            return

        self.history_cache_key = task.cache_key
        self.history_cache_locations = list(task.locations)
