"""
main.py
───────
Convenience entry point – delegates to app.py.
Run:  python main.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import main

if __name__ == "__main__":
    main()
