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

# Robust distance (in MAD-scaled standard deviations, averaged
# over features) above which an observation counts as unusual.
# A fixed cut-off means a dataset with no unusual behaviour
# flags nothing, instead of always flagging its top 10%.
ANOMALY_DISTANCE = 3.5

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

    return (values - center) / spread


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


def run_anomaly_detection(df):
    """
    Add:
        anomaly_score  (0 typical -> 1 highly unusual)
        is_anomaly

    Uses robust statistical distance from the normal
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

    scaled = robust_scale(
        values
    )


    # ---------------------------------------------------------
    # ANOMALY SCORES
    # ---------------------------------------------------------

    distances = _calculate_distances(
        scaled
    )

    df["anomaly_score"] = np.round(
        np.clip(distances / SCORE_SCALE, 0, 1),
        4
    )

    df["is_anomaly"] = distances >= ANOMALY_DISTANCE

    return df
