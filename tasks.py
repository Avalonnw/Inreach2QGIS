# -*- coding: utf-8 -*-

from qgis.core import QgsTask


class GarminRefreshTask(QgsTask):
    """Run Garmin current-position requests away from the QGIS GUI thread."""

    def __init__(self, client, imeis, generation, callback):
        super().__init__(
            'Refresh Garmin inReach positions',
            QgsTask.Flag.CanCancel | QgsTask.Flag.Silent,
        )
        self.client = client
        self.imeis = list(imeis)
        self.generation = generation
        self.callback = callback
        self.locations = []
        self.error = None

    def run(self):
        if self.isCanceled():
            return False
        try:
            self.locations = self.client.last_known_locations(self.imeis)
        except Exception as exc:
            self.error = str(exc)
            return False
        return not self.isCanceled()

    def finished(self, result):
        callback = self.callback
        self.callback = None
        if callback is not None:
            callback(self, result)


class GarminHistoryTask(QgsTask):
    """Run Garmin location-history requests away from the QGIS GUI thread."""

    def __init__(self, client, imeis, start, end, cache_key, generation, callback):
        super().__init__(
            'Load Garmin inReach history',
            QgsTask.Flag.CanCancel | QgsTask.Flag.Silent,
        )
        self.client = client
        self.imeis = list(imeis)
        self.start = start
        self.end = end
        self.cache_key = cache_key
        self.generation = generation
        self.callback = callback
        self.locations = []
        self.error = None
        self.failures = []

    def run(self):
        if self.isCanceled():
            return False

        # Fetch each device independently. Garmin may return an empty/null History
        # result, or even a device-specific error, for one IMEI while other selected
        # devices have valid data. One such device must not discard everybody else's
        # History response or prevent their map layers from being completed.
        successful_requests = 0
        for imei in self.imeis:
            if self.isCanceled():
                return False
            try:
                locations = self.client.history([imei], self.start, self.end)
            except Exception as exc:
                self.failures.append((str(imei), str(exc)))
                continue

            successful_requests += 1
            if locations:
                self.locations.extend(locations)

        if self.failures and successful_requests == 0:
            details = '; '.join(
                f'{imei}: {error}'
                for imei, error in self.failures
            )
            self.error = f'Garmin History failed for all selected devices: {details}'
            return False

        return not self.isCanceled()

    def finished(self, result):
        callback = self.callback
        self.callback = None
        if callback is not None:
            callback(self, result)
