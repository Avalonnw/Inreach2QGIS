# -*- coding: utf-8 -*-

from qgis.core import QgsFeature, QgsProject, QgsVectorLayer

from .api import GarminApiError
from .plugin import KIND_HISTORY_POINTS, KIND_HISTORY_TRACKS
from .persistent_plugin import (
    TABLE_NAMES,
    PersistentInreach2QGISPlugin,
)


class SafePersistentInreach2QGISPlugin(PersistentInreach2QGISPlugin):
    """Fail closed around project-storage rebinding and derived track rebuilds."""

    def _is_foreign_persistent_layer(self, layer):
        """Return True when a managed OGR layer still points at another project."""
        target = self._storage_path()
        if not target or layer is None or layer.providerType() != 'ogr':
            return False
        return self._layer_storage_path(layer) != self._normalized_path(target)

    def _history_archive_ready(self, layer):
        """Require real project-local OGR storage once the project is saved."""
        target = self._storage_path()
        if not target:
            # Unsaved projects intentionally use temporary memory layers.
            return True
        return (
            layer is not None
            and layer.providerType() == 'ogr'
            and self._layer_storage_path(layer) == self._normalized_path(target)
        )

    @staticmethod
    def _drop_tracks_layer(layer):
        """Hide failed/stale derived tracks without deleting the GeoPackage table."""
        if layer is None:
            return
        project = QgsProject.instance()
        if project.mapLayer(layer.id()) is not None:
            project.removeMapLayer(layer.id())

    def _reject_foreign_archive(self, layer_name):
        self._storage_error(
            f'Persistent storage rebind failed for {layer_name}. '
            'The previous project GeoPackage will not be modified. '
            'Fix access to the new project storage and refresh again.'
        )

    def _reject_history_archive(self, layer_name):
        self._storage_error(
            f'Persistent History storage is unavailable for {layer_name}. '
            'The Garmin History response was not accepted or cached because the '
            'saved project is not writing to its own GeoPackage. Fix access to '
            'the project storage and refresh again.'
        )

    def refresh_history_if_enabled(self):
        """Hide stale History immediately when the configured range is invalid."""
        if (
            not self._unloading
            and self.store.project_enabled()
            and self.store.project_history_enabled()
        ):
            selected = self.store.project_imeis()
            if selected:
                try:
                    # Validate before the persistent refresh loads/reuses any local
                    # History view. The parent implementation intentionally keeps
                    # local data visible on network failures, but an invalid range
                    # describes no valid view and must not leave the previous one
                    # on screen.
                    PersistentInreach2QGISPlugin._history_request(self, selected)
                except GarminApiError as exc:
                    self._remove_history_layers()
                    self.iface.messageBar().pushCritical(
                        'Inreach2QGIS history',
                        str(exc),
                    )
                    return

        return super().refresh_history_if_enabled()

    def _history_feature_key(self, feature):
        return self._history_record_key(
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

    def _merge_memory_history_into_existing(self, source_layer, path):
        """Merge temporary or foreign History points into an existing project table."""
        table_name = TABLE_NAMES[KIND_HISTORY_POINTS]
        stored = QgsVectorLayer(
            f'{path}|layername={table_name}',
            'History points',
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
        for source in source_layer.getFeatures():
            key = self._history_feature_key(source)
            if key in existing:
                continue
            existing.add(key)
            feature = QgsFeature(stored.fields())
            feature.setGeometry(source.geometry())
            feature.setAttributes(source.attributes())
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

    def _adopt_existing_storage_layer(self, source_layer, kind):
        """Replace a managed source layer with the existing project-local table."""
        project = QgsProject.instance()
        old_node = project.layerTreeRoot().findLayer(source_layer.id())
        old_visible = old_node.itemVisibilityChecked() if old_node is not None else True
        old_expanded = old_node.isExpanded() if old_node is not None else True

        stored = self._load_storage_layer(kind)
        if stored is None:
            raise RuntimeError(
                f'Could not adopt persistent Garmin storage for {TABLE_NAMES[kind]}.'
            )

        if project.mapLayer(source_layer.id()) is not None:
            project.removeMapLayer(source_layer.id())
        self._organize_managed_layers()

        new_node = project.layerTreeRoot().findLayer(stored.id())
        if new_node is not None:
            new_node.setItemVisibilityChecked(old_visible)
            new_node.setExpanded(old_expanded)
        return stored

    def _persist_memory_layer(self, layer, kind):
        """Preserve History and restore the active view after rebinding."""
        if layer is None:
            return layer

        path = self._storage_path()
        provider_type = layer.providerType()
        old_subset = (
            str(layer.subsetString() or '')
            if provider_type == 'ogr' and kind == KIND_HISTORY_POINTS
            else ''
        )
        adopted_memory_tracks = bool(
            path
            and provider_type == 'memory'
            and kind == KIND_HISTORY_TRACKS
        )

        # Save As can target a basename whose companion GeoPackage already exists.
        # For persistent History points, never let the base implementation overwrite
        # that destination table. Merge the complete old-project archive into the
        # existing destination, then adopt the destination layer. This preserves
        # records that exist only in either archive.
        if (
            path
            and provider_type == 'ogr'
            and kind == KIND_HISTORY_POINTS
            and self._layer_storage_path(layer) != self._normalized_path(path)
            and self._table_exists(path, TABLE_NAMES[KIND_HISTORY_POINTS])
        ):
            subset_cleared = False
            try:
                if old_subset:
                    if not layer.setSubsetString(''):
                        raise RuntimeError(
                            'Could not clear the History points filter before '
                            'merging the Save As archive.'
                        )
                    subset_cleared = True

                self._merge_memory_history_into_existing(layer, path)
                stored = self._adopt_existing_storage_layer(
                    layer,
                    KIND_HISTORY_POINTS,
                )
            except Exception as exc:
                if subset_cleared:
                    layer.setSubsetString(old_subset)
                self._storage_error(exc)
                return layer

            if subset_cleared:
                layer.setSubsetString(old_subset)

            if old_subset:
                if not stored.setSubsetString(old_subset):
                    self._storage_error(
                        'Could not restore the Garmin History filter after merging '
                        'Save As storage.'
                    )
            elif self.store.project_history_enabled():
                selected = self.store.project_imeis()
                if selected:
                    try:
                        _, start_text, end_text = self._history_request(selected)
                    except Exception:
                        pass
                    else:
                        self._apply_history_subset(
                            stored,
                            selected,
                            start_text,
                            end_text,
                        )
            return stored

        # A project may be saved under a basename whose companion GeoPackage
        # already exists. The base persistence path intentionally reuses an
        # existing table, so merge temporary History first instead of silently
        # replacing the memory layer with that table and losing fetched points.
        if (
            path
            and provider_type == 'memory'
            and kind == KIND_HISTORY_POINTS
            and self._table_exists(path, TABLE_NAMES[KIND_HISTORY_POINTS])
        ):
            try:
                self._merge_memory_history_into_existing(layer, path)
            except Exception as exc:
                self._storage_error(exc)
                return layer

        stored = super()._persist_memory_layer(layer, kind)

        # Save As clears the source subset so the complete archive is copied. The
        # replacement OGR layer must regain that filter; otherwise all archived
        # points suddenly become visible while tracks still represent only the
        # selected device/date range. Memory -> existing-table transitions have no
        # subset to copy, so derive the current History view from project settings.
        if (
            kind == KIND_HISTORY_POINTS
            and stored is not None
            and stored.providerType() == 'ogr'
            and self._history_archive_ready(stored)
        ):
            if old_subset:
                if not stored.setSubsetString(old_subset):
                    self._storage_error(
                        'Could not restore the Garmin History filter after rebinding '
                        'persistent storage.'
                    )
            elif self.store.project_history_enabled():
                selected = self.store.project_imeis()
                if selected:
                    try:
                        _, start_text, end_text = self._history_request(selected)
                    except Exception:
                        pass
                    else:
                        self._apply_history_subset(
                            stored,
                            selected,
                            start_text,
                            end_text,
                        )

        # History tracks are derived state, never the source of truth. When an
        # unsaved project's temporary tracks become persistent, rebuild them from
        # the already persisted/merged History points instead of trusting an
        # existing history_tracks table that may belong to an older project view.
        if adopted_memory_tracks:
            if stored is None or not self._history_archive_ready(stored):
                # Points may already have been merged into an existing archive. A
                # temporary track can therefore be incomplete and must not remain
                # visible when its own persistent rebind failed.
                self._drop_tracks_layer(stored or layer)
                return stored

            if self.store.project_history_enabled():
                selected = self.store.project_imeis()
                if selected:
                    try:
                        _, start_text, end_text = self._history_request(selected)
                    except GarminApiError:
                        self._drop_tracks_layer(stored)
                    else:
                        points_layer = self._get_or_create_history_points_layer()
                        if not self._history_archive_ready(points_layer):
                            self._drop_tracks_layer(stored)
                            self._reject_history_archive('History points')
                        elif not self._rebuild_tracks_from_local(
                            points_layer,
                            stored,
                            selected,
                            start_text,
                            end_text,
                        ):
                            self._drop_tracks_layer(stored)
                        else:
                            self._organize_managed_layers()

        return stored

    def _apply_locations(self, locations, selected):
        # _get_or_create_current_layer() performs the Save As rebind attempt. If
        # that attempt failed, the base implementation leaves the old OGR layer
        # available. Never pass that layer into the mutable current-position path.
        layer = self._get_or_create_current_layer()
        if self._is_foreign_persistent_layer(layer):
            self._reject_foreign_archive('Current location')
            return False
        return super()._apply_locations(locations, selected)

    def _apply_history_subset(self, layer, selected, start_text, end_text):
        # GeoPackage layers can use a provider subset without deleting archived
        # data. An unsaved project has only a temporary memory layer, so there is
        # no durable archive to preserve: remove rows outside the active range and
        # device selection so old temporary points cannot remain visible after the
        # user changes History settings.
        if layer is None or layer.providerType() != 'memory':
            return super()._apply_history_subset(
                layer,
                selected,
                start_text,
                end_text,
            )

        remove_ids = [
            feature.id()
            for feature in layer.getFeatures()
            if not self._history_row_in_range(
                feature,
                selected,
                start_text,
                end_text,
            )
        ]
        if not remove_ids:
            return True

        if not layer.dataProvider().deleteFeatures(remove_ids):
            # Memory-layer deletion should normally be reliable. If it is not,
            # fail closed by clearing the temporary layer rather than displaying
            # stale points from a previous device/date selection.
            if not self._clear_layer(layer):
                self._storage_error(
                    'Could not filter temporary Garmin History points for the '
                    'new device/date selection.'
                )
                return False

        layer.updateExtents()
        layer.triggerRepaint()
        return True

    def _rebuild_tracks_from_local(
        self,
        points_layer,
        tracks_layer,
        selected,
        start_text,
        end_text,
    ):
        # A failed Save As rebind must never turn the old project's derived track
        # table into a write target. Also fail closed when the local-view rebuild
        # itself fails: stale tracks from a previous range/device selection are
        # more dangerous than temporarily showing no track at all.
        if (
            self._is_foreign_persistent_layer(points_layer)
            or self._is_foreign_persistent_layer(tracks_layer)
        ):
            self._drop_tracks_layer(tracks_layer)
            self._reject_foreign_archive('History')
            return False

        ok = super()._rebuild_tracks_from_local(
            points_layer,
            tracks_layer,
            selected,
            start_text,
            end_text,
        )
        if not ok:
            self._drop_tracks_layer(tracks_layer)
        return ok

    def _apply_history(self, locations, selected):
        # Preflight both History layers after their persistence/rebind attempt.
        # Once the QGIS project has a filename, a memory fallback is not an
        # archive: do not append fetched History, report it as archived, or cache
        # the successful network response unless both layers are OGR-backed by the
        # current project's companion GeoPackage.
        points_layer = self._get_or_create_history_points_layer()
        if not self._history_archive_ready(points_layer):
            if self._is_foreign_persistent_layer(points_layer):
                self._reject_foreign_archive('History points')
            else:
                self._reject_history_archive('History points')
            return False

        tracks_layer = self._get_or_create_history_tracks_layer()
        if not self._history_archive_ready(tracks_layer):
            self._drop_tracks_layer(tracks_layer)
            if self._is_foreign_persistent_layer(tracks_layer):
                self._reject_foreign_archive('History tracks')
            else:
                self._reject_history_archive('History tracks')
            return False

        return super()._apply_history(locations, selected)
