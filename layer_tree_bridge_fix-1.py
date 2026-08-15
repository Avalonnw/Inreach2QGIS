# -*- coding: utf-8 -*-

from qgis.core import QgsFeature, QgsProject

from .plugin import (
    CURRENT_LAYER_NAME,
    HISTORY_POINTS_LAYER_NAME,
    HISTORY_TRACKS_LAYER_NAME,
    KIND_CURRENT,
    KIND_HISTORY_POINTS,
    KIND_HISTORY_TRACKS,
)
from .runtime_fixes import RobustInreach2QGISPlugin


_HISTORY_POINT_FIELDS = (
    'name',
    'imei',
    'time_utc',
    'time_local',
    'latitude',
    'longitude',
    'altitude_m',
    'speed_kmh',
    'course_deg',
    'gps_fix',
    'message',
)

_HISTORY_TRACK_FIELDS = (
    'name',
    'imei',
    'start_utc',
    'end_utc',
    'point_count',
)


class BridgeSafeInreach2QGISPlugin(RobustInreach2QGISPlugin):
    """Keep QGIS layer-tree and persistent provider writes bridge-safe."""

    def __init__(self, iface):
        super().__init__(iface)
        self._normalize_history_provider_writes = True

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

        wanted_ids = [layer.id() for layer in ordered]
        if not wanted_ids:
            return

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

        # QgsLayerTreeRegistryBridge removes a QgsMapLayer from the project when
        # its final legend node disappears. Therefore recovery must follow QGIS'
        # drag/drop-safe order: insert a replacement first, then remove old nodes.
        for index, layer in enumerate(ordered):
            layer_id = layer.id()
            nodes = [
                node for node in list(root.findLayers())
                if node.layerId() == layer_id
            ]

            if not nodes:
                group.insertLayer(index, layer)
                continue

            # If a stray duplicate exists, preserve the node which is already in
            # the plugin-owned Garmin group instead of whichever root traversal
            # happens to return first.
            canonical_nodes = [
                child
                for child in group.children()
                if hasattr(child, 'layerId') and child.layerId() == layer_id
            ]
            source_node = canonical_nodes[0] if canonical_nodes else nodes[0]
            replacement = source_node.clone()
            try:
                group.insertChildNode(index, replacement)
            except Exception:
                # Nothing has been removed yet, so a failed insert is harmless.
                continue

            # The replacement already exists in the tree, so removing the old
            # node(s) cannot make the layer disappear from the tree/registry.
            for old_node in nodes:
                parent = old_node.parent()
                if parent is not None:
                    parent.removeChildNode(old_node)

    def _history_provider_kind(self, provider):
        names = {field.name() for field in provider.fields()}
        if set(_HISTORY_POINT_FIELDS).issubset(names):
            return KIND_HISTORY_POINTS
        if set(_HISTORY_TRACK_FIELDS).issubset(names):
            return KIND_HISTORY_TRACKS
        return None

    def _normalize_positional_history_features(self, provider, features, kind):
        """Convert old positional History features into typed, name-based features."""
        field_order = (
            _HISTORY_POINT_FIELDS
            if kind == KIND_HISTORY_POINTS
            else _HISTORY_TRACK_FIELDS
        )
        rebuilt = []

        for source in features:
            raw = list(source.attributes())
            # Persistent History builders pass exactly the logical schema values
            # to setAttributes(). If that ever changes, fail closed rather than
            # guessing at a different positional layout.
            if len(raw) != len(field_order):
                raise RuntimeError(
                    f'Unexpected Garmin {kind} attribute count: '
                    f'{len(raw)} instead of {len(field_order)}.'
                )

            values = dict(zip(field_order, raw))
            if kind == KIND_HISTORY_POINTS:
                values = {
                    'name': str(values.get('name') or ''),
                    'imei': str(values.get('imei') or ''),
                    'time_utc': str(values.get('time_utc') or ''),
                    'time_local': str(values.get('time_local') or ''),
                    'latitude': self._history_value(values.get('latitude'), numeric=True),
                    'longitude': self._history_value(values.get('longitude'), numeric=True),
                    'altitude_m': self._history_value(values.get('altitude_m'), numeric=True),
                    'speed_kmh': self._history_value(values.get('speed_kmh'), numeric=True),
                    'course_deg': self._history_value(values.get('course_deg'), numeric=True),
                    'gps_fix': self._history_value(values.get('gps_fix'), integer=True),
                    'message': str(values.get('message') or ''),
                }
            else:
                values = {
                    'name': str(values.get('name') or ''),
                    'imei': str(values.get('imei') or ''),
                    'start_utc': str(values.get('start_utc') or ''),
                    'end_utc': str(values.get('end_utc') or ''),
                    'point_count': self._history_value(
                        values.get('point_count'),
                        integer=True,
                    ),
                }

            feature = QgsFeature(provider.fields())
            feature.setGeometry(source.geometry())
            self._set_attributes_by_name(feature, values)
            rebuilt.append(feature)

        return rebuilt

    def _provider_add_features(self, provider, features):
        kind = self._history_provider_kind(provider)
        if (
            self._normalize_history_provider_writes
            and kind is not None
            and features
        ):
            try:
                features = self._normalize_positional_history_features(
                    provider,
                    features,
                    kind,
                )
            except Exception as exc:
                self._storage_error(
                    f'Could not normalize Garmin {kind} attributes: {exc}'
                )
                return False, 0
        return super()._provider_add_features(provider, features)

    def _merge_memory_history_into_existing(self, source_layer, path):
        # RobustInreach2QGISPlugin already builds merge features by field name.
        # Do not reinterpret those correct provider-order attributes as the legacy
        # positional History layout handled above.
        previous = self._normalize_history_provider_writes
        self._normalize_history_provider_writes = False
        try:
            return super()._merge_memory_history_into_existing(source_layer, path)
        finally:
            self._normalize_history_provider_writes = previous
