"""Wspolny mechanizm przerwania (Ctrl-C) dla pracy wielowatkowej.

Problem: time.sleep() w limiterze/backoffie potrafi blokowac watek nawet do
kilkudziesieciu sekund, a ThreadPoolExecutor przy wyjsciu czeka na wszystkie
zakolejkowane zadania. Skutek: Ctrl-C "nie dziala" (program wisi).

Rozwiazanie: globalne zdarzenie STOP + przerywalny sen. Petle robocze sprawdzaja
STOP, a sleepy budza sie co krok i sprawdzaja, czy nie zlecono zatrzymania.
"""

import threading
import time

# Ustawiane przy pierwszym Ctrl-C (przez petle glowne). Watki to respektuja.
STOP = threading.Event()


def interruptible_sleep(seconds: float, step: float = 0.2):
    """Sen, ktory natychmiast (do `step` s) reaguje na STOP."""
    if seconds <= 0:
        return
    end = time.monotonic() + seconds
    while not STOP.is_set():
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(step, remaining))


def stopping() -> bool:
    return STOP.is_set()


def request_stop():
    STOP.set()


class Interrupted(BaseException):
    """Sygnal przerwania w watku roboczym.

    Dziedziczy po BaseException (NIE Exception), wiec zwykle ``except Exception``
    w watkach go nie przechwyci - dzieki temu praca konczy sie szybko zamiast
    byc traktowana jak blad sieci (i niepotrzebnie ponawiana/zapisywana).
    """

