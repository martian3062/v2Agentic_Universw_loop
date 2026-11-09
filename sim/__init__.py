"""Closed-loop simulator for the outcome-first agent loop.

Treat this package as the outside world: your controller may only talk to it
over HTTP (`server.py`) and read the event files it writes. Do not import it
from controller code.
"""
