# Inreach2QGIS

QGIS plugin for displaying Garmin inReach Professional device positions and location history.

**Tested with:** QGIS 3.44.x and QGIS 4.2.x (Windows)

Features:

- Garmin IPC Inbound V1 using HTTP Basic login/password.
- Garmin IPC Inbound V2 using `X-API-Key`.
- Credentials stored in the encrypted QGIS Authentication Database.
- Global device registry (`Display name` + `IMEI` + history colour).
- CSV import/export of the device registry, including history colour.
- Per-project device selection stored inside the QGIS project.
- A new/empty QGIS project does **not** automatically load every configured Garmin device.
- Automatic current-position refresh with Fresh / Delayed / Stale status.
- Current positions are 6 mm Arrow markers rotated from Garmin `Course`, while retaining Fresh / Delayed / Stale / Unknown colours.
- Optional per-project location history with an inclusive date range.
- Persistent project-local GeoPackage storage for Current location, History points and History tracks.
- Garmin History is merged into the local archive; previously saved history is not deleted when Garmin later returns less data.
- History is fetched in a QGIS background task and rendered as both point and track layers.
- History tracks are rebuilt from the locally stored History points archive.
- History tracks and direction markers use the configured colour for each IMEI.
- History point markers are small direction arrows rotated from Garmin `Course`.
- Zero-coordinate history placeholders are ignored so tracks do not jump to `0,0`.
- Exact duplicate Garmin history records are removed before local archive insertion.
- All managed map layers are kept in an owned `Garmin inReach` layer-tree group.
- Plugin commands are available from `Plugins > InReach2QGIS > Setup` and `Plugins > InReach2QGIS > Refresh`; the plugin does not create a toolbar.

## Project behaviour

The global registry is only an address book. Each `.qgz` project separately stores:

- whether Garmin tracking is enabled for that project;
- which IMEIs from the registry belong to that project;
- whether history should be loaded;
- the requested history start/end dates.

Once the QGIS project has been saved to disk, Inreach2QGIS creates a GeoPackage beside it:

```text
ExampleProject.qgz
ExampleProject_inreach.gpkg
```

The GeoPackage contains these plugin-managed layers:

- `current_location`
- `history_points`
- `history_tracks`

The corresponding QGIS layer-tree presentation remains:

- `Current location`
- `History points`
- `History tracks`

If the QGIS project has not yet been saved, Garmin layers remain temporary and the plugin shows a warning. As soon as the project has a filename, existing temporary managed layers are migrated to the project GeoPackage on the next plugin refresh/history load.

`Current location` contains only the selected devices and is refreshed automatically. Its 6 mm Arrow markers are rotated from `course_deg`; marker colour continues to indicate Fresh / Delayed / Stale / Unknown status and is not coloured by IMEI. The latest current positions are also stored in the project GeoPackage, so the last known positions can remain available when the project is reopened offline.

`History points` is the durable source of truth for the project history. Every Garmin History response is deduplicated against the full local archive and only genuinely new records are appended. Existing local points are never removed merely because Garmin no longer returns them. `History tracks` is derived data: it is rebuilt from the locally archived points for the currently selected devices and history date range.

Changing the history date/device selection changes what is displayed and how tracks are rebuilt, but older locally archived History points remain in the GeoPackage. Disabling tracking/history removes the managed layers from the QGIS project, not the underlying GeoPackage data; re-enabling them reloads the saved archive.

The plugin preserves layer-tree visibility/check state when it moves or reorders existing managed layers. It identifies its own group by an ownership property, so a user-created group which happens to also be named `Garmin inReach` is not adopted or deleted.

The regular refresh timer only updates current positions. History is fetched once when the project/settings are loaded and cached for the QGIS session. Before the network request, the plugin loads the local History archive, so saved history remains visible if Garmin is offline, credentials fail, or older upstream data has disappeared. Reopening QGIS or switching projects causes a fresh History request. Changing settings within the same session invalidates the History cache before reloading.

The History `to` date in the UI is inclusive. Internally the plugin sends the following calendar day as Garmin's `End` boundary so the whole selected end date is included.

## Device CSV format

The values below are examples only.

```csv
name,imei,color
Device A,123456789012345,#2563EB
Device B,123456789012346,#DC2626
```

Import merges by IMEI. Export never contains passwords or API keys.

## 0.5.1 changes

- Added dual QGIS 3 / QGIS 4 compatibility by migrating Qt and QGIS enum usage to scoped forms supported by PyQt5 and PyQt6.
- Added a Qt5/Qt6-compatible `QAction` import and replaced the legacy `QDialog.exec_()` call with `exec()`.
- Declared QGIS 4 compatibility through `qgisMaximumVersion=4.99`.
- Runtime-tested the same plugin build in QGIS 3.44.x and QGIS 4.2.x on Windows, including Setup, Current/Refresh, History and project/GeoPackage save-reopen behaviour.

## 0.5.0 changes

- Added a concise Plugin Manager quick-start explaining where to find `Setup` and `Refresh` after installation.
- Reordered the Current project settings so tracking/history controls and the History date range appear above the per-project device selection.
- Promoted the tested 0.4.x work to the 0.5.0 milestone without changing Garmin API, archive, refresh, styling or project-selection behaviour.

## 0.4.1 fixes

- Made Garmin History loading tolerant of partial, empty and null per-device responses while keeping failed devices retryable.
- Fixed managed layer-tree recovery so QGIS does not remove plugin layers during group reordering or duplicate recovery.
- Fixed typed GeoPackage writes for Current and History layers, including numeric `age_min` and integer `gps_fix` fields.

## 0.4.0 changes

- Added project-local persistent GeoPackage storage beside the saved `.qgz` project.
- Current location, History points and History tracks are file-backed after the project is saved.
- Existing temporary managed layers are migrated to the GeoPackage when persistent storage becomes available.
- Garmin History responses are merged into the local History points archive instead of replacing it.
- Local history survives upstream Garmin/MapShare cleanup or incomplete later History responses.
- History tracks are rebuilt from locally archived points for the active device/date selection.
- Existing local history is loaded before Garmin is contacted, so it remains visible while offline or when the History request fails.
- Disabling/removing managed layers from the project does not delete their underlying GeoPackage archive.

## 0.3.2 changes

- Current positions use 6 mm course-rotated Arrow markers while retaining Fresh / Delayed / Stale / Unknown colours.
- Managed layers are grouped under `Garmin inReach` with `Current location` above `History points` and `History tracks`.
- Existing managed Current location layers are restyled during migration so projects saved with 0.3.1 pick up the Arrow renderer immediately.
- Layer visibility/check state is preserved while managed layers are moved or reordered.
- A user-created same-name group is left untouched; the plugin only manages a group carrying its ownership property.
- The plugin no longer creates a toolbar. `Setup` and `Refresh` are available under `Plugins > InReach2QGIS`.

## 0.3.1 fixes

- Garmin history placeholders at latitude/longitude `0,0` are ignored.
- Each device can have its own history colour in the Devices registry.
- Device colours are included in CSV import/export.
- History points are rendered as small course-direction markers.

## 0.2.1 fixes

- Automatic Garmin polling runs as a QGIS background task instead of blocking the GUI thread.
- A device added manually in the registry becomes available on the Current project tab immediately when that tab is opened.
- Clearing a migrated V2 API key no longer resurrects the legacy key.
- Authentication-database writes are attempted before ordinary/project settings are saved, avoiding half-saved configuration when the auth store is locked or unavailable.

## Development

Changes are developed in topic branches and merged to `main` through pull requests after testing.
