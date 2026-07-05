"""Macro Context Monitor.

A read-only observation-and-hypothesis-proposal layer over internal-research-service's paper-traded
strategies. It collects macro/market context (RSS feeds, WebSearch), correlates it read-only
with recorded strategy behavior in paper.db, and — no more than weekly — proposes candidate
hypotheses for a human to run through the existing /spec-gather pipeline.

Hard boundary (FR-12): this package NEVER makes, influences, or automates a live or paper
trading decision, and never writes to paper.db. Its only output is a written report.
"""

__version__ = "0.1.0"
