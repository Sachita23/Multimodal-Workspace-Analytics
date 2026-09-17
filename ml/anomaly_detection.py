import numpy as np
import pandas as pd


MODEL_FEATURES = [
    "idle_ratio",
    "switch_frequency",
    "unique_app_count",
    "avg_cpu",
    "avg_memory",
]

# Avoid pretending anomaly detection is meaningful
# with extremely small datasets.
MIN_SAMPLES = 20


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


def _standardize(values):
    """
    Standardize values using NumPy.
    """

    mean = values.mean(
        axis=0
    )

    std = values.std(
        axis=0
    )

    # Prevent division by zero.
    std[std == 0] = 1.0

    scaled = (
        values - mean
    ) / std

    return scaled


def _calculate_distance_scores(values):
    """
    Measure how far each observation is from
    the typical behavioral profile.

    Output range:
        0 -> very typical
        1 -> highly unusual
    """

    if len(values) == 0:
        return np.array([])

    center = np.median(
        values,
        axis=0
    )

    distances = np.linalg.norm(
        values - center,
        axis=1
    )

    # If every observation is effectively identical,
    # there is no evidence of an anomaly.
    if np.allclose(
        distances,
        distances[0]
    ):
        return np.zeros(
            len(distances),
            dtype=float
        )

    low = np.percentile(
        distances,
        5
    )

    high = np.percentile(
        distances,
        95
    )

    if high <= low:
        return np.zeros(
            len(distances),
            dtype=float
        )

    scores = (
        distances - low
    ) / (
        high - low
    )

    scores = np.clip(
        scores,
        0,
        1
    )

    return scores


def run_anomaly_detection(df):
    """
    Add:
        anomaly_score
        is_anomaly

    Uses statistical distance from the normal
    behavioral profile.
    """

    df = df.copy()

    # ---------------------------------------------------------
    # INSUFFICIENT HISTORY
    # ---------------------------------------------------------

    if len(df) < MIN_SAMPLES:

        df["anomaly_score"] = 0.0
        df["is_anomaly"] = False

        return df


    # ---------------------------------------------------------
    # FEATURE PREPARATION
    # ---------------------------------------------------------

    features = _prepare_features(
        df
    )

    values = features.to_numpy(
        dtype=float
    )

    scaled = _standardize(
        values
    )


    # ---------------------------------------------------------
    # ANOMALY SCORES
    # ---------------------------------------------------------

    scores = _calculate_distance_scores(
        scaled
    )

    df["anomaly_score"] = np.round(
        scores,
        4
    )


    # ---------------------------------------------------------
    # HANDLE NO-VARIATION CASE
    # ---------------------------------------------------------

    if len(scores) == 0 or np.allclose(
        scores,
        0
    ):

        df["is_anomaly"] = False

        return df


    # ---------------------------------------------------------
    # ANOMALY THRESHOLD
    # ---------------------------------------------------------

    threshold = np.percentile(
        scores,
        90
    )

    # Require both:
    # 1. score above threshold
    # 2. score has some meaningful magnitude
    df["is_anomaly"] = (
        (scores > threshold)
        &
        (scores >= 0.60)
    )

    return df