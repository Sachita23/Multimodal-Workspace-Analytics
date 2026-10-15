import numpy as np
import pandas as pd

from config import ANOMALY_DISTANCE
from etl.session_processor import build_session_table


MODEL_FEATURES = [
    "idle_ratio",
    "switch_frequency",
    "unique_app_count",
    "avg_cpu",
    "avg_memory",
]

# Avoid pretending anomaly detection is meaningful
# with extremely small datasets: rows, for frames without
# sessions, and finished sessions otherwise.
MIN_SAMPLES = 20
MIN_SESSIONS = 8

# ANOMALY_DISTANCE (config.py) is the robust distance, in
# MAD-scaled standard deviations averaged over features, above
# which a session counts as unusual. A fixed cut-off means a
# dataset with no unusual behaviour flags nothing, instead of
# always flagging its top 10%.

# Distance that maps to an anomaly_score of 1.0.
SCORE_SCALE = ANOMALY_DISTANCE * 2


def _prepare_features(df):
    """
    Prepare numerical behavioral features.
    """

    features = df[
        MODEL_FEATURES
    ].copy()

    for column in MODEL_FEATURES:

        features[column] = pd.to_numeric(
            features[column],
            errors="coerce"
        )

    features = features.replace(
        [np.inf, -np.inf],
        np.nan
    )

    features = features.fillna(
        features.median(
            numeric_only=True
        )
    )

    features = features.fillna(0)

    return features


def robust_scale(values):
    """
    Scale each column by its median and MAD.

    MAD is used instead of the standard deviation so a few
    extreme observations cannot hide themselves by inflating
    the spread. When over half the values are identical the MAD
    is 0, so the mean absolute deviation is used instead
    (Iglewicz & Hoaglin); columns with no spread at all get 1.
    """

    center, spread = _robust_fit(values)

    return (values - center) / spread


def _robust_fit(values):
    """
    Per-column median and robust spread (see robust_scale).
    """

    center = np.median(
        values,
        axis=0
    )

    deviation = np.abs(values - center)

    mad = np.median(
        deviation,
        axis=0
    ) * 1.4826

    mean_ad = deviation.mean(
        axis=0
    ) * 1.2533

    spread = np.where(mad > 0, mad, mean_ad)
    spread = np.where(spread > 0, spread, 1.0)

    return center, spread


def _calculate_distances(scaled):
    """
    Root-mean-square robust z-score per observation.

    0 -> exactly typical, larger -> more unusual.
    """

    if len(scaled) == 0:
        return np.array([])

    return np.sqrt(
        np.mean(
            scaled ** 2,
            axis=1
        )
    )


def _score(distances):

    return np.round(
        np.clip(distances / SCORE_SCALE, 0, 1),
        4
    )


def run_anomaly_detection(df, now=None):
    """
    Add:
        anomaly_score  (0 typical -> 1 highly unusual)
        is_anomaly

    Uses robust statistical distance from the normal
    behavioral profile.

    With a session_id column, every session is scored once and
    all its rows share that decision. Only finished sessions of
    at least MIN_SESSION_SECONDS are fitted and flagged; the
    still-open session and very short ones get score 0 and
    is_anomaly False. Without session_id, each row is scored.
    """

    df = df.copy()

    if "session_id" not in df.columns:
        return _score_rows(df)

    df["anomaly_score"] = 0.0
    df["is_anomaly"] = False

    sessions = build_session_table(
        df,
        MODEL_FEATURES,
        now=now
    )

    eligible = sessions[sessions["eligible"]]


    # ---------------------------------------------------------
    # INSUFFICIENT HISTORY
    # ---------------------------------------------------------

    if len(eligible) < MIN_SESSIONS:
        return df


    # ---------------------------------------------------------
    # ONE SCORE PER FINISHED SESSION
    # ---------------------------------------------------------

    values = _prepare_features(
        eligible
    ).to_numpy(
        dtype=float
    )

    distances = _calculate_distances(
        robust_scale(values)
    )

    scores = pd.Series(
        _score(distances),
        index=eligible.index
    )

    flagged = pd.Series(
        distances >= ANOMALY_DISTANCE,
        index=eligible.index
    )

    in_eligible = df["session_id"].isin(eligible.index)

    df.loc[in_eligible, "anomaly_score"] = (
        df.loc[in_eligible, "session_id"].map(scores)
    )

    df.loc[in_eligible, "is_anomaly"] = (
        df.loc[in_eligible, "session_id"].map(flagged)
    )

    df["is_anomaly"] = df["is_anomaly"].astype(bool)

    return df


def _score_rows(df):
    """
    Per-row scoring, for frames that carry only the features.
    """

    if len(df) < MIN_SAMPLES:

        df["anomaly_score"] = 0.0
        df["is_anomaly"] = False

        return df

    values = _prepare_features(
        df
    ).to_numpy(
        dtype=float
    )

    distances = _calculate_distances(
        robust_scale(values)
    )

    df["anomaly_score"] = _score(distances)

    df["is_anomaly"] = distances >= ANOMALY_DISTANCE

    return df
