"""Outcome-first controller: detects, experiments, proposes through governance, applies, explains.

It talks to the world only over the simulator's HTTP API (docs/SIMULATOR.md). All state lives in one
SQLite file so the process can be killed at any point and resumed.
"""
