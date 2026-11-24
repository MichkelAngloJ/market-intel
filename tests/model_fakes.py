"""A small synthetic dataset for the model tests: fake stocks in three sectors, features and
21-day labels built with the real feature and label code. Made-up prices only."""

from __future__ import annotations

from datetime import date

import pandas as pd
from feature_fakes import business_days, candles

from market_intel import features as F
from market_intel.feature_spec import TRAINING_FEATURES
from market_intel.models import Dataset, build_dataset

SECTORS = ("Fake Sector A", "Fake Sector B", "Fake Sector C")


def dataset(n_days: int = 900, stocks: int = 12, start: date = date(2022, 1, 3)) -> Dataset:
    days = business_days(start, n_days)
    nifty = candles(days, 900, 20000.0)
    vix = candles(days, 901, 15.0)
    frames = {100 + i: candles(days, 1000 + i, 100.0 + 5 * i) for i in range(stocks)}
    sector_of = {inst: SECTORS[i % 3] for i, inst in enumerate(frames)}
    inputs = F.Inputs(
        candles=frames,
        sector_of=sector_of,
        nifty=nifty,
        vix=vix,
        calendar=list(days),
        symbols={k: f"FAKE{k}" for k in frames},
    )
    market, sectors = inputs.market(), inputs.sectors()
    parts = []
    for inst, frame in frames.items():
        feats = F.build_features(frame, market, sectors[sector_of[inst]])[list(TRAINING_FEATURES)]
        labels = F.build_labels(frame, days, horizons=(21,)).set_index("trade_date")
        part = feats.join(labels[["fwd_return", "up"]])
        part.insert(0, "sector", sector_of[inst])
        part.insert(0, "instrument_id", inst)
        parts.append(part.reset_index(names="trade_date"))
    panel = pd.concat(parts, ignore_index=True)
    return build_dataset(panel, list(days), frames, nifty, {}, inputs.symbols)
