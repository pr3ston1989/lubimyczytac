"""Wspolny mechanizm przerwania (Ctrl-C) dla pracy wielowatkowej.

Problem: time.sleep() w limiterze/backoffie potrafi blokowac watek nawet do
kilkudziesieciu sekund, a ThreadPoolExecutor przy wyjsciu czeka na wszystkie
zakolejkowane zadania. Skutek: Ctrl-C "nie dziala" (program wisi).

Rozwiazanie: globalne zdarzenie STOP + przerywalny sen. Petle robocze sprawdzaja
STOP, a sleepy budza sie co krok i sprawdzaja, czy nie zlecono zatrzymania.
"""

import threading
import time
import signal
from concurrent.futures import wait, FIRST_COMPLETED

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


def iter_done(futures, poll: float = 0.5):
    """Jak concurrent.futures.as_completed, ale BUDZI SIE co `poll` s i sprawdza
    STOP. Dzieki temu Ctrl-C dziala takze na Windows (gdzie blokada bez timeoutu
    nie jest przerywana przez SIGINT). Po STOP po prostu przestaje wydawac wyniki.
    """
    pending = set(futures)
    while pending:
        if STOP.is_set():
            return
        done, pending = wait(pending, timeout=poll, return_when=FIRST_COMPLETED)
        for fut in done:
            yield fut


def install_sigint():
    """Pierwsze Ctrl-C ustawia STOP (lagodne zatrzymanie); drugie - twardo
    przerywa (KeyboardInterrupt). Dziala tylko w watku glownym; w razie czego
    zwraca None. Zwraca poprzedni handler do przywrocenia."""
    def _handler(signum, frame):
        if STOP.is_set():
            raise KeyboardInterrupt()
        STOP.set()
    try:
        return signal.signal(signal.SIGINT, _handler)
    except (ValueError, OSError, RuntimeError):
        return None


def restore_sigint(previous):
    if previous is None:
        return
    try:
        signal.signal(signal.SIGINT, previous)
    except (ValueError, OSError, RuntimeError, TypeError):
        pass


class Interrupted(BaseException):
    """Sygnal przerwania w watku roboczym.

    Dziedziczy po BaseException (NIE Exception), wiec zwykle ``except Exception``
    w watkach go nie przechwyci - dzieki temu praca konczy sie szybko zamiast
    byc traktowana jak blad sieci (i niepotrzebnie ponawiana/zapisywana).
    """

