"""Globalny, wspoldzielony limiter szybkosci zadan HTTP.

Problem: przy 6+ watkach kazdy watek wysylal zadania niezaleznie, wiec laczny
ruch przekraczal tolerancje serwera -> masowe HTTP 429 (Too Many Requests).
Per-watkowe `time.sleep` nie ogranicza LACZNEJ liczby zadan na sekunde
(rosnie liniowo z liczba watkow).

Rozwiazanie: jeden limiter wspoldzielony przez wszystkie watki Scrapera.
Kazdy watek "rezerwuje" przyszly slot czasowy pod lockiem, a usypia juz POZA
lockiem - dzieki temu zadania startuja rownolegle, ale tempo ich wypuszczania
jest globalnie ograniczone (~`requests_per_second`), niezaleznie od liczby
watkow.

Dodatkowo limiter jest ADAPTACYJNY: po HTTP 429/503 zwieksza odstep miedzy
zadaniami (penalize), a z czasem powoli wraca do bazowego tempa (recover).
"""

import threading
import time
import random


class RateLimiter:
    def __init__(self, requests_per_second: float = 2.0, jitter: float = 0.1,
                 max_interval: float = 30.0):
        rps = requests_per_second if requests_per_second > 0 else 2.0
        self._base_interval = 1.0 / rps
        self._interval = self._base_interval
        self._max_interval = max_interval
        self._jitter = jitter
        self._next_time = 0.0
        self._lock = threading.Lock()

    def wait(self):
        """Blokuje az do przydzielonego slotu czasowego (bezpieczne dla watkow)."""
        with self._lock:
            now = time.monotonic()
            scheduled = max(now, self._next_time)
            interval = self._interval
            if self._jitter:
                interval += random.uniform(0, self._jitter)
            self._next_time = scheduled + interval
        delay = scheduled - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def penalize(self, factor: float = 2.0):
        """Po 429/503 zwieksza odstep miedzy zadaniami (do max_interval)."""
        with self._lock:
            self._interval = min(self._interval * factor, self._max_interval)

    def recover(self, factor: float = 0.9):
        """Po serii sukcesow stopniowo wraca do bazowego tempa."""
        with self._lock:
            self._interval = max(self._interval * factor, self._base_interval)

    @property
    def current_interval(self) -> float:
        return self._interval
