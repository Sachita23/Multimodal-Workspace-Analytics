import itertools
import json
import math

import numpy as np
import pandas as pd

from config import (
    DEFAULT_CLUSTER_COUNT,
    RANDOM_STATE
)
from etl.session_processor import build_session_table


MODEL_FEATURES = [
    "idle_ratio",
    "switch_frequency",
    "unique_app_count",
    "avg_cpu",
    "avg_memory",
]

# Fewer finished sessions than this and there is nothing to group.
MIN_SESSIONS = 4

# Each cluster should describe at least this many sessions.
MIN_SESSIONS_PER_CLUSTER = 2

# K-Means restarts; the run with the lowest inertia wins.
N_INIT = 10

# Labels of sessions that are not clustered (cluster_id -1).
NOT_ENOUGH_HISTORY = "Not-enough-history"
IN_PROGRESS = "In-progress"      # the session still being recorded
TOO_SHORT = "Too-short"          # finished, under MIN_SESSION_SECONDS

# A behaviour label is only given to a cluster whose center actually
# shows that behaviour, so a calm one-app cluster is never called
# "Multitasking" just because it ranked first among what was left.
IDLE_HEAVY_MIN_RATIO = 0.5
HIGH_SWITCHING_MIN_PER_MINUTE = 0.4
MULTITASKING_MIN_APPS = 3
MULTITASKING_MIN_PER_MINUTE = 0.1


# ---------------------------------------------------------
# Stored model
# ---------------------------------------------------------
#
# The clusters are kept between pipeline runs (run_pipeline.py saves
# them to config.BEHAVIOR_MODEL_FILE) so finished sessions keep their
# cluster and label while new sessions close:
#
#   - a finished session that was assigned once keeps that cluster;
#   - new finished sessions take the nearest stored cluster;
#   - the model is refitted only when the finished history has grown
#     by REFIT_GROWTH (or the number of clusters changes). A refit
#     starts from the stored centers, so clusters keep their ids and
#     sessions mostly stay put; names are only re-checked then.

MODEL_VERSION = 1

REFIT_GROWTH = 1.25

# A refit keeps the clusters grown from the stored centers unless a
# fresh fit is this much tighter (lower inertia).
WARM_START_TOLERANCE = 0.05

# At a refit a cluster keeps its label unless another cluster is
# better for it by more than this many standard deviations.
LABEL_HYSTERESIS = 0.5

# Sessions are remembered by their first timestamp, which does not
# change as new samples are appended.
SESSION_KEY_FORMAT = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------
# Features
# ---------------------------------------------------------

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


# ---------------------------------------------------------
# K-Means (k-means++ initialisation, several restarts)
# ---------------------------------------------------------

def _squared_distances(values, centroids):

    return (
        (
            values[:, np.newaxis, :]
            - centroids[np.newaxis, :, :]
        ) ** 2
    ).sum(axis=2)


def _kmeans_plus_plus(values, k, rng):
    """
    Spread the initial centroids out: each next centroid is
    picked with probability proportional to its squared
    distance from the nearest centroid chosen so far.
    """
    n_samples = len(values)

    centroids = [values[rng.integers(n_samples)]]

    for _ in range(1, k):

        distances = _squared_distances(
            values,
            np.array(centroids)
        ).min(axis=1)

        total = distances.sum()

        if total <= 0:
            # Every point sits on a centroid already.
            index = rng.integers(n_samples)
        else:
            index = rng.choice(
                n_samples,
                p=distances / total
            )

        centroids.append(values[index])

    return np.array(centroids, dtype=float)


def _lloyd(values, centroids, max_iterations):

    k = len(centroids)

    for _ in range(max_iterations):

        labels = np.argmin(
            _squared_distances(values, centroids),
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
            centroids = new_centroids
            break

        centroids = new_centroids

    distances = _squared_distances(values, centroids)
    labels = np.argmin(distances, axis=1)
    inertia = float(distances[np.arange(len(values)), labels].sum())

    return labels, centroids, inertia


def _kmeans(
    values,
    k,
    max_iterations=100,
    n_init=N_INIT,
    random_state=RANDOM_STATE
):
    """
    NumPy K-Means: k-means++ initialisation, `n_init` seeded
    restarts, best inertia kept. Deterministic for a given input.

    Returns (labels, centroids).
    """
    values = np.asarray(values, dtype=float)
    n_samples = len(values)

    if n_samples < k:
        k = n_samples

    if k <= 0:
        return np.array([], dtype=int), np.empty((0, values.shape[1]))

    rng = np.random.default_rng(random_state)

    best = None

    for _ in range(max(1, n_init)):

        initial = _kmeans_plus_plus(values, k, rng)

        labels, centroids, inertia = _lloyd(
            values,
            initial,
            max_iterations
        )

        if best is None or inertia < best[2] - 1e-9:
            best = (labels, centroids, inertia)

    return best[0], best[1]


def _inertia(values, labels, centroids):

    if len(values) == 0:
        return 0.0

    return float(((values - centroids[labels]) ** 2).sum())


def _add_farthest(values, centroids, k):
    """
    `centroids` plus, until there are k, the point of `values`
    farthest from every center chosen so far (deterministic).
    """
    centroids = np.asarray(centroids, dtype=float)

    while len(centroids) < k:
        distances = _squared_distances(values, centroids).min(axis=1)
        centroids = np.vstack([centroids, values[np.argmax(distances)]])

    return centroids


def _canonical_order(labels, centroids, mean, std):
    """
    Renumber clusters by idle_ratio, then switch_frequency, so the
    same behaviour keeps the same cluster_id (and dashboard colour)
    from one refresh to the next.
    """
    original = centroids * std + mean

    order = np.lexsort((
        original[:, MODEL_FEATURES.index("switch_frequency")],
        original[:, MODEL_FEATURES.index("idle_ratio")],
    ))

    remap = np.empty(len(order), dtype=int)
    remap[order] = np.arange(len(order))

    return remap[labels], centroids[order]


def _match_clusters(new, old):
    """
    Pair new cluster centers with old ones (same scaled space) so the
    total squared distance is smallest. Returns {new index: old index};
    with different counts, the extra centers stay unpaired.
    """
    new = np.asarray(new, dtype=float)
    old = np.asarray(old, dtype=float)

    if len(new) == 0 or len(old) == 0:
        return {}

    cost = _squared_distances(new, old)
    small, large = sorted((len(new), len(old)))

    if math.perm(large, small) <= 40320:

        best_total, best = None, None

        if len(new) <= len(old):
            options = (
                {i: j for i, j in enumerate(perm)}
                for perm in itertools.permutations(range(len(old)), len(new))
            )
        else:
            options = (
                {i: j for j, i in enumerate(perm)}
                for perm in itertools.permutations(range(len(new)), len(old))
            )

        for pairs in options:
            total = sum(cost[i, j] for i, j in pairs.items())

            if best_total is None or total < best_total - 1e-12:
                best_total, best = total, pairs

        return best

    # Many clusters: closest pairs first.
    pairs = {}
    used = set()

    for _, i, j in sorted(
        (cost[i, j], i, j)
        for i in range(len(new))
        for j in range(len(old))
    ):
        if i not in pairs and j not in used:
            pairs[i] = j
            used.add(j)

    return pairs


# ---------------------------------------------------------
# Labels
# ---------------------------------------------------------

def _assign_behavior_label(center, mean, std):
    """
    Label ONE cluster center from absolute thresholds.

    Kept for callers that label centers one at a time (team
    analysis merges clusters that share a label). Prefer
    assign_labels(), which ranks the centers against each other.
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


def label_fits(name, idle_ratio, switch_frequency, unique_apps):
    """
    Whether a cluster center with these (unscaled) values actually
    shows the behaviour `name` describes.
    """

    if name == "Idle-heavy":
        return idle_ratio > IDLE_HEAVY_MIN_RATIO

    if name == "High-switching":
        return switch_frequency >= HIGH_SWITCHING_MIN_PER_MINUTE

    if name == "Multitasking":
        return (
            unique_apps >= MULTITASKING_MIN_APPS
            and switch_frequency >= MULTITASKING_MIN_PER_MINUTE
        )

    if name == "Stable-activity":
        return (
            idle_ratio <= IDLE_HEAVY_MIN_RATIO
            and switch_frequency < HIGH_SWITCHING_MIN_PER_MINUTE
        )

    return str(name).startswith("Mixed-activity")


def assign_labels(centroids, mean, std, previous=None):
    """
    One behaviour label per cluster center, distinct except that
    every mostly-idle center is "Idle-heavy".

    These labels describe the user's own patterns relative to
    each other; they do not claim that an application is
    inherently productive or unproductive.

    Each label goes to the center that ranks highest for it among
    those not yet labelled, but only if that center shows the
    behaviour (label_fits):

        Idle-heavy       highest idle_ratio, above 0.5
        High-switching   highest switch_frequency, >= 0.4 / min
        Multitasking     most apps, >= 3 apps and >= 0.1 switches / min
        Stable-activity  lowest switch_frequency, mostly active and
                         below the high-switching rate
        Idle-heavy       any other center above 0.5 idle
        Mixed-activity   anything left ("Mixed-activity 2", ... after it)

    `previous` lists the label each center had in the stored model
    (or None). A center keeps its label while it still fits and no
    other center is clearly better for it (by LABEL_HYSTERESIS), so
    two similar clusters do not swap names on every refit.
    """
    centroids = np.asarray(centroids, dtype=float)

    if len(centroids) == 0:
        return []

    original = centroids * std + mean

    def column(name):
        index = MODEL_FEATURES.index(name)
        return original[:, index], centroids[:, index]

    idle, idle_scaled = column("idle_ratio")
    switching, switching_scaled = column("switch_frequency")
    apps, apps_scaled = column("unique_app_count")

    previous = list(previous or [])
    previous += [None] * (len(centroids) - len(previous))

    def fits(name, i):
        return label_fits(name, idle[i], switching[i], apps[i])

    labels = [None] * len(centroids)
    remaining = list(range(len(centroids)))

    def take(name, scores, pick=max):
        candidates = [i for i in remaining if fits(name, i)]

        if not candidates:
            return

        index = pick(candidates, key=lambda i: (scores[i], -i))

        holders = [i for i in candidates if previous[i] == name]

        if holders and abs(
            scores[holders[0]] - scores[index]
        ) <= LABEL_HYSTERESIS:
            index = holders[0]

        labels[index] = name
        remaining.remove(index)

    take("Idle-heavy", idle_scaled)
    take("High-switching", switching_scaled)
    take("Multitasking", apps_scaled)
    take("Stable-activity", switching_scaled, pick=min)

    # A second mostly-idle cluster is still idle-heavy; calling it
    # "Mixed-activity" would contradict its own features.
    for index in list(remaining):
        if fits("Idle-heavy", index):
            labels[index] = "Idle-heavy"
            remaining.remove(index)

    mixed = ["Mixed-activity"] + [
        f"Mixed-activity {number}"
        for number in range(2, len(centroids) + 1)
    ]

    # Left-over clusters keep their old "Mixed-activity N" name.
    for index in list(remaining):
        if previous[index] in mixed:
            labels[index] = previous[index]
            mixed.remove(previous[index])
            remaining.remove(index)

    for index, name in zip(remaining, mixed):
        labels[index] = name

    return labels


# ---------------------------------------------------------
# Stored model helpers
# ---------------------------------------------------------

def load_model(path):
    """
    The stored model (a dict) from `path`, or None when it is
    missing or unreadable.
    """
    try:
        with open(path, encoding="utf-8") as file:
            model = json.load(file)

    except (OSError, ValueError):
        return None

    return model if isinstance(model, dict) else None


def _check_model(model):
    """
    A validated copy of a stored model, or None if it cannot be used
    (another version or feature list, or damaged).
    """
    if not isinstance(model, dict):
        return None

    try:
        if model["version"] != MODEL_VERSION:
            return None

        if list(model["features"]) != MODEL_FEATURES:
            return None

        width = len(MODEL_FEATURES)

        mean = np.asarray(model["mean"], dtype=float)
        std = np.asarray(model["std"], dtype=float)

        clusters = [
            {
                "id": int(cluster["id"]),
                "label": str(cluster["label"]),
                "centroid": np.asarray(cluster["centroid"], dtype=float),
            }
            for cluster in model["clusters"]
        ]

        sessions = {
            str(key): int(value)
            for key, value in dict(model["sessions"]).items()
        }

        ids = {cluster["id"] for cluster in clusters}

        if (
            mean.shape != (width,)
            or std.shape != (width,)
            or not np.all(np.isfinite(mean))
            or not np.all(std > 0)
            or not clusters
            or len(ids) != len(clusters)
            or any(cluster["centroid"].shape != (width,) for cluster in clusters)
            or not set(sessions.values()) <= ids
        ):
            return None

        return {
            "k": int(model["k"]),
            "fitted": int(model["fitted"]),
            "mean": mean,
            "std": std,
            "clusters": clusters,
            "sessions": sessions,
        }

    except (KeyError, TypeError, ValueError):
        return None


def _model_dict(k, fitted, mean, std, clusters, sessions):
    """
    The JSON-ready stored model (plain Python types only, so a model
    read back from disk compares equal to the one written).
    """
    return {
        "version": MODEL_VERSION,
        "features": list(MODEL_FEATURES),
        "k": int(k),
        "fitted": int(fitted),
        "mean": [float(value) for value in mean],
        "std": [float(value) for value in std],
        "clusters": [
            {
                "id": int(cluster["id"]),
                "label": str(cluster["label"]),
                "centroid": [float(value) for value in cluster["centroid"]],
            }
            for cluster in sorted(clusters, key=lambda c: c["id"])
        ],
        "sessions": {
            str(key): int(value)
            for key, value in sorted(sessions.items())
        },
    }


# ---------------------------------------------------------
# Session clustering
# ---------------------------------------------------------

def _fit(values, k, previous):
    """
    Fit k clusters to `values` (finished sessions, unscaled).

    Returns (labels as cluster ids, clusters, mean, std) where each
    cluster is {"id", "label", "centroid" (unscaled)}. Ids and names
    of `previous` clusters carry over to the clusters they match.
    """
    scaled, mean, std = _standardize(values)

    labels, centroids = _kmeans(scaled, k)
    labels, centroids = _canonical_order(labels, centroids, mean, std)

    old_ids, old_names, pairs = [], [], {}

    if previous is not None:

        old_ids = [cluster["id"] for cluster in previous["clusters"]]
        old_names = [cluster["label"] for cluster in previous["clusters"]]

        old_scaled = np.array([
            (cluster["centroid"] - mean) / std
            for cluster in previous["clusters"]
        ])

        # Grow the stored centers on the new history first: as long as
        # the behaviour is the same, sessions stay where they were.
        # When there are more clusters now, the extra ones start at
        # the sessions farthest from the stored centers.
        if len(old_scaled) <= len(centroids):

            warm_labels, warm_centroids, warm_inertia = _lloyd(
                scaled,
                _add_farthest(scaled, old_scaled, len(centroids)),
                100
            )

            every_cluster_used = (
                len(np.unique(warm_labels)) == len(warm_centroids)
            )

            if every_cluster_used and warm_inertia <= (
                _inertia(scaled, labels, centroids)
                * (1 + WARM_START_TOLERANCE)
                + 1e-9
            ):
                labels, centroids = warm_labels, warm_centroids
                pairs = {i: i for i in range(len(old_scaled))}

        if not pairs:
            pairs = _match_clusters(centroids, old_scaled)

    # Matched clusters keep their stored id (and, if it still fits,
    # their name); new ones take the lowest free ids.
    ids = [None] * len(centroids)
    old_labels = [None] * len(centroids)

    for new_index, old_index in pairs.items():
        ids[new_index] = old_ids[old_index]
        old_labels[new_index] = old_names[old_index]

    free = (number for number in itertools.count() if number not in ids)

    for index in range(len(ids)):
        if ids[index] is None:
            ids[index] = next(free)

    # Names are decided once per fit; between fits the centers, and so
    # the names of past sessions, stay as stored.
    names = assign_labels(centroids, mean, std, previous=old_labels)

    clusters = [
        {
            "id": ids[index],
            "label": names[index],
            "centroid": centroids[index] * std + mean,
        }
        for index in range(len(centroids))
    ]

    return np.array(ids, dtype=int)[labels], clusters, mean, std


def _nearest(values, clusters, mean, std):
    """
    Id of the nearest stored cluster for each row of `values`.
    """
    centers = np.array([
        (cluster["centroid"] - mean) / std
        for cluster in clusters
    ])

    ids = np.array([cluster["id"] for cluster in clusters], dtype=int)

    nearest = np.argmin(
        _squared_distances((values - mean) / std, centers),
        axis=1
    )

    return ids[nearest]


def cluster_sessions(sessions, model=None):
    """
    Cluster one row per session (build_session_table output with
    MODEL_FEATURES) using the stored `model` from an earlier run.

    Returns (cluster_ids, labels, new_model): two Series indexed like
    `sessions`, and the model to store (None while there is too
    little history).

    Clustered: finished sessions of at least MIN_SESSION_SECONDS,
    plus sessions the model already knows. Every other session gets
    cluster_id -1 and IN_PROGRESS (the open session) or TOO_SHORT.
    """
    cluster_ids = pd.Series(-1, index=sessions.index, dtype=int)
    labels = pd.Series(TOO_SHORT, index=sessions.index, dtype=object)

    if sessions.empty:
        return cluster_ids, labels, None

    labels[sessions["is_open"].to_numpy(dtype=bool)] = IN_PROGRESS

    keys = sessions["start"].dt.strftime(SESSION_KEY_FORMAT).to_numpy()
    values = _prepare_features(sessions).to_numpy(dtype=float)

    previous = _check_model(model)

    # A stored session that no longer exists means the history was
    # replaced (new raw file): start over.
    if previous is not None and not set(previous["sessions"]) <= set(keys):
        previous = None

    stored = previous["sessions"] if previous is not None else {}

    known = np.array([key in stored for key in keys], dtype=bool)

    # Finished history. A known session counts even when a later
    # sample (e.g. a dark wake while the user is away) reopened it.
    history = sessions["eligible"].to_numpy(dtype=bool) | known

    n_history = int(history.sum())

    if n_history < MIN_SESSIONS:
        labels[:] = NOT_ENOUGH_HISTORY
        return cluster_ids, labels, None

    k = min(
        DEFAULT_CLUSTER_COUNT,
        n_history // MIN_SESSIONS_PER_CLUSTER
    )

    refit = (
        previous is None
        or k != previous["k"]
        or n_history >= math.ceil(previous["fitted"] * REFIT_GROWTH)
    )

    if refit:

        history_ids, clusters, mean, std = _fit(
            values[history],
            k,
            previous
        )

        fitted = n_history

        assigned = dict(zip(keys[history], history_ids))

    else:

        clusters = previous["clusters"]
        mean, std = previous["mean"], previous["std"]
        fitted = previous["fitted"]

        assigned = dict(stored)

        new = history & ~known

        if new.any():
            assigned.update(zip(
                keys[new],
                _nearest(values[new], clusters, mean, std)
            ))

    names = {cluster["id"]: cluster["label"] for cluster in clusters}

    for position in np.flatnonzero(history):
        cluster_id = int(assigned[keys[position]])
        cluster_ids.iloc[position] = cluster_id
        labels.iloc[position] = names[cluster_id]

    new_model = _model_dict(k, fitted, mean, std, clusters, assigned)

    return cluster_ids, labels, new_model


# ---------------------------------------------------------
# Pipeline step
# ---------------------------------------------------------

def cluster_with_model(df, now=None, model=None):
    """
    Add cluster_id and behavior_label to the dataframe, starting from
    the stored `model` (see load_model). Returns (df, new_model).

    Clusters one row per session (features are per session, so
    long sessions must not outweigh short ones).
    """
    df = df.copy()

    sessions = build_session_table(
        df,
        MODEL_FEATURES,
        now=now
    )

    cluster_ids, labels, new_model = cluster_sessions(sessions, model)

    df["cluster_id"] = (
        df["session_id"]
        .map(cluster_ids)
        .astype(int)
    )

    df["behavior_label"] = (
        df["session_id"]
        .map(labels)
        .astype(object)
    )

    return df, new_model


def run_clustering(df, now=None, model=None):
    """
    Add cluster_id and behavior_label to the dataframe.

    Without a stored model every call fits from scratch; the
    pipeline passes the model from its previous run (see
    cluster_with_model) so past sessions keep their labels.
    """
    return cluster_with_model(df, now=now, model=model)[0]
