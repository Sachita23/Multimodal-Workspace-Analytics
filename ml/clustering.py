from pathlib import Path

import numpy as np
import pandas as pd


MODEL_FEATURES = [
    "idle_ratio",
    "switch_frequency",
    "unique_app_count",
    "avg_cpu",
    "avg_memory",
]

MIN_SAMPLES = 8
N_CLUSTERS = 4


def _prepare_features(df):
    """
    Prepare numerical behavioral features for clustering.
    """
    features = df[MODEL_FEATURES].copy()

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
        features.median(numeric_only=True)
    )

    features = features.fillna(0)

    return features


def _standardize(values):
    """
    Standardize features without sklearn.
    """
    mean = values.mean(axis=0)
    std = values.std(axis=0)

    std[std == 0] = 1

    scaled = (values - mean) / std

    return scaled, mean, std


def _kmeans(values, k, max_iterations=100):
    """
    Simple NumPy implementation of K-Means clustering.
    """
    n_samples = len(values)

    if n_samples < k:
        k = n_samples

    # Deterministic initialization.
    indices = np.linspace(
        0,
        n_samples - 1,
        k,
        dtype=int
    )

    centroids = values[indices].copy()

    for _ in range(max_iterations):

        distances = np.linalg.norm(
            values[:, np.newaxis, :] -
            centroids[np.newaxis, :, :],
            axis=2
        )

        labels = np.argmin(
            distances,
            axis=1
        )

        new_centroids = centroids.copy()

        for cluster_id in range(k):
            members = values[labels == cluster_id]

            if len(members) > 0:
                new_centroids[cluster_id] = members.mean(axis=0)

        if np.allclose(
            centroids,
            new_centroids,
            atol=1e-5
        ):
            break

        centroids = new_centroids

    return labels, centroids


def _assign_behavior_label(center, mean, std):
    """
    Convert a numerical cluster center into a descriptive
    behavior label.

    These labels describe observed patterns; they do not
    claim that an application is inherently productive
    or unproductive.
    """
    original = center * std + mean

    idle_ratio = original[0]
    switch_frequency = original[1]
    unique_apps = original[2]

    if idle_ratio >= 0.60:
        return "Idle-heavy"

    if switch_frequency >= 0.20:
        return "High-switching"

    if unique_apps >= 3:
        return "Multitasking"

    return "Stable-activity"


def run_clustering(df):
    """
    Add cluster_id and behavior_label to the dataframe.

    Uses a lightweight NumPy K-Means implementation.
    """
    df = df.copy()

    if len(df) < MIN_SAMPLES:
        df["cluster_id"] = -1
        df["behavior_label"] = "Not-enough-history"
        return df

    features = _prepare_features(df)

    values = features.to_numpy(dtype=float)

    scaled, mean, std = _standardize(values)

    k = min(
        N_CLUSTERS,
        len(values)
    )

    labels, centroids = _kmeans(
        scaled,
        k
    )

    label_map = {}

    for cluster_id, center in enumerate(centroids):
        label_map[cluster_id] = _assign_behavior_label(
            center,
            mean,
            std
        )

    df["cluster_id"] = labels.astype(int)

    df["behavior_label"] = [
        label_map.get(
            int(cluster_id),
            "Unknown"
        )
        for cluster_id in labels
    ]

    return df   