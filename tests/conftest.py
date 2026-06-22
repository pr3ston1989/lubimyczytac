import os
import sys

# Pozwala uruchamiac testy z dowolnego katalogu - dodaje root projektu do sys.path.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
