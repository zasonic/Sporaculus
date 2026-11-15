"""Bounded contexts of the modular monolith.

Each module owns its service logic + its router. Modules talk to each other
through explicit function calls (never through each other's tables), which is
what makes a later split into services mechanical rather than archaeological.
"""
