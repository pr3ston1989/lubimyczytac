"""Globalny, wspoldzielony limiter szybkosci zadan HTTP (algorytm AIMD).

Problem: przy wielu watkach kazdy watek wysylal zadania niezaleznie, wiec laczny
ruch przekraczal tolerancje serwera -> masowe HTTP 429. Per-watkowe `time.sleep`
nie ogranicza LACZNEJ liczby zadan/s (rosnie liniowo z liczba watkow).

Rozwiazanie: jeden limiter wspoldzielony przez wszystkie watki. Kazdy watek
"rezerwuje" przyszly slot czasowy pod lockiem, a usypia POZA lockiem - zadania
startuja rownolegle, ale tempo ich wypuszczania jest globalnie ograniczone.

ADAPTACJA (AIMD = Additive Increase / Multiplicative Decrease tempa):
  * po HTTP 429/503: odstep miedzy zadaniami jest MNOZONY (gwaltowne zwolnienie),
  * po sukcesie: odstep maleje TYLKO o maly, staly krok (powolny powrot).
To kluczowe: gdyby powrot byl mnozeniem (np. *0.9), kilkanascie rownoczesnych
sukcesow natychmiast kasowaloby kare i serwer znow zwracalby 429 (oscylacja).
Dzieki AIMD tempo samo ustala sie tuz PONIZEJ progu blokady serwera.
"""

import threading
import time
import random


class RateLimiter:
    def __init__(self, requests_per_second: float = 1.0, jitter: float = 0.1,
                 max_interval: float = 60.0, increase_factor: float = 2.0,
                 decrease_ratio: float = 0.1):
        rps = requests_per_second if requests_per_second > 0 else 1.0
        self._base_interval = 1.0 / rps
        self._interval = self._base_interval
        self._max_interval = max(max_interval, self._base_interval)
        self._increase_factor = increase_factor
        # Krok addytywnego powrotu: ulamek bazowego odstepu (powolny, stabilny).
        self._decrease_step = max(self._base_interval * decrease_ratio, 0.02)
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

    def penalize(self):
        """Po 429/503 MNOZY odstep (gwaltowne zwolnienie, do max_interval)."""
        with self._lock:
            self._interval = min(self._interval * self._increase_factor, self._max_interval)

    def recover(self):
        """Po sukcesie zmniejsza odstep o JEDEN maly krok (powolny powrot)."""
        with self._lock:
            self._interval = max(self._interval - self._decrease_step, self._base_interval)

    @property
    def current_interval(self) -> float:
        return self._interval

    @property
    def current_rps(self) -> float:
        return 1.0 / self._interval if self._interval > 0 else 0.0
