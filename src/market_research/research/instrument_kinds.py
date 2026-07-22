"""Neutral instrument-kind vocabulary shared by research domain contracts."""

from __future__ import annotations

from enum import StrEnum


class InstrumentKind(StrEnum):
    SPOT = "SPOT"
    EQUITY = "EQUITY"
    ETF = "ETF"
    INDEX = "INDEX"
    FUTURE = "FUTURE"
    OPTION = "OPTION"
    RATE = "RATE"
    FX = "FX"
    COMMODITY = "COMMODITY"
