# Agent-written blueprints and modules (C5)

Everything here was written by the portal's drafter, not by a person, and is
kept apart from `orchestrator/blueprints/` for exactly that reason. Discovery
marks each entry with its origin, and a generated blueprint may not take the
ref or resource kind of a shipped one.

This directory is mounted into the orchestrator at `/generated`. It is not part
of the image: a module written into the image at run time would be invisible to
discovery and would disappear on the next restart.
