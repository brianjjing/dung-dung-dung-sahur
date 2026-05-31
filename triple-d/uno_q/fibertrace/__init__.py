"""FiberTrace — offline edge-AI fiber-tether detection + operator guesstimate.

"Most counter-drone systems look up. FiberTrace looks down the line."

The RC car watches a fiber-optic drone, detects the thin fiber trailing from
it, and extrapolates that line to guesstimate where the operator is. A second
capability for the same car/Uno Q used by Triple D; reuses the shared config.py.

Run from triple-d/uno_q:
    python -m fibertrace.trace_main
"""
