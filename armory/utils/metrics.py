"""
Metrics for scenarios

Outputs are lists of python variables amenable to JSON serialization:
    e.g., bool, int, float
    numpy data types and tensors generally fail to serialize
"""

import logging
import numpy as np
import time
from contextlib import contextmanager
import cProfile
import pstats
import io
from collections import defaultdict


logger = logging.getLogger(__name__)


def categorical_accuracy(y, y_pred):
    """
    Return the categorical accuracy of the predictions
    """
    y = np.asarray(y)
    y_pred = np.asarray(y_pred)
    if y.ndim == 0:
        y = np.array([y])
        y_pred = np.array([y_pred])

    if y.shape == y_pred.shape:
        return [int(x) for x in list(y == y_pred)]
    elif y.ndim + 1 == y_pred.ndim:
        if y.ndim == 0:
            return [int(y == np.argmax(y_pred, axis=-1))]
        return [int(x) for x in list(y == np.argmax(y_pred, axis=-1))]
    else:
        raise ValueError(f"{y} and {y_pred} have mismatched dimensions")


def top_5_categorical_accuracy(y, y_pred):
    """
    Return the top 5 categorical accuracy of the predictions
    """
    return top_n_categorical_accuracy(y, y_pred, 5)


def top_n_categorical_accuracy(y, y_pred, n):
    if n < 1:
        raise ValueError(f"n must be a positive integer, not {n}")
    n = int(n)
    if n == 1:
        return categorical_accuracy(y, y_pred)
    y = np.asarray(y)
    y_pred = np.asarray(y_pred)
    if y.ndim == 0:
        y = np.array([y])
        y_pred = np.array([y_pred])

    if len(y) != len(y_pred):
        raise ValueError("y and y_pred are of different length")
    if y.shape == y_pred.shape:
        raise ValueError("Must supply multiple predictions for top 5 accuracy")
    elif y.ndim + 1 == y_pred.ndim:
        y_pred_top5 = np.argsort(y_pred, axis=-1)[:, -n:]
        if y.ndim == 0:
            return [int(y in y_pred_top5)]
        return [int(y[i] in y_pred_top5[i]) for i in range(len(y))]
    else:
        raise ValueError(f"{y} and {y_pred} have mismatched dimensions")


def norm(x, x_adv, ord):
    """
    Return the given norm over a batch, outputting a list of floats
    """
    x = np.asarray(x)
    x_adv = np.asarray(x_adv)
    # cast to float first to prevent overflow errors
    diff = (x.astype(float) - x_adv.astype(float)).reshape(x.shape[0], -1)
    values = np.linalg.norm(diff, ord=ord, axis=1)
    return list(float(x) for x in values)


def linf(x, x_adv):
    """
    Return the L-infinity norm over a batch of inputs as a float
    """
    return norm(x, x_adv, np.inf)


def l2(x, x_adv):
    """
    Return the L2 norm over a batch of inputs as a float
    """
    return norm(x, x_adv, 2)


def l1(x, x_adv):
    """
    Return the L1 norm over a batch of inputs as a float
    """
    return norm(x, x_adv, 1)


def lp(x, x_adv, p):
    """
    Return the Lp norm over a batch of inputs as a float
    """
    if p <= 0:
        raise ValueError(f"p must be positive, not {p}")
    return norm(x, x_adv, p)


def l0(x, x_adv):
    """
    Return the L0 'norm' over a batch of inputs as a float
    """
    return norm(x, x_adv, 0)


def _snr(x_i, x_adv_i):
    x_i = np.asarray(x_i, dtype=float)
    x_adv_i = np.asarray(x_adv_i, dtype=float)
    if x_i.shape != x_adv_i.shape:
        raise ValueError(f"x_i.shape {x_i.shape} != x_adv_i.shape {x_adv_i.shape}")
    elif x_i.ndim != 1:
        raise ValueError("_snr input must be single dimensional (not multichannel)")
    signal_power = (x_i ** 2).mean()
    noise_power = ((x_i - x_adv_i) ** 2).mean()
    return signal_power / noise_power


def snr(x, x_adv):
    """
    Return the SNR of a batch of samples with raw audio input
    """
    if len(x) != len(x_adv):
        raise ValueError(f"len(x) {len(x)} != len(x_adv) {len(x_adv)}")
    return [float(_snr(x_i, x_adv_i)) for (x_i, x_adv_i) in zip(x, x_adv)]


def snr_db(x, x_adv):
    """
    Return the SNR of a batch of samples with raw audio input in Decibels (DB)
    """
    return [float(i) for i in 10 * np.log10(snr(x, x_adv))]


def _snr_spectrogram(x_i, x_adv_i):
    x_i = np.asarray(x_i, dtype=float)
    x_adv_i = np.asarray(x_adv_i, dtype=float)
    if x_i.shape != x_adv_i.shape:
        raise ValueError(f"x_i.shape {x_i.shape} != x_adv_i.shape {x_adv_i.shape}")
    signal_power = np.abs(x_i).mean()
    noise_power = np.abs(x_i - x_adv_i).mean()
    return signal_power / noise_power


@contextmanager
def resource_context(name="Name", profiler=None, computational_resource_dict=None):
    if profiler is None:
        yield
        return 0
    profiler_types = ["Basic", "Deterministic"]
    if profiler is not None and profiler not in profiler_types:
        raise ValueError(f"Profiler {profiler} is not one of {profiler_types}.")
    if profiler == "Deterministic":
        logger.warn(
            "Using Deterministic profiler. This may reduce timing accuracy and result in a large results file."
        )
        pr = cProfile.Profile()
        pr.enable()
    startTime = time.perf_counter()
    yield
    elapsedTime = time.perf_counter() - startTime
    if profiler == "Deterministic":
        pr.disable()
        s = io.StringIO()
        sortby = "cumulative"
        ps = pstats.Stats(pr, stream=s).sort_stats(sortby)
        ps.print_stats()
        stats = s.getvalue()
    if name not in computational_resource_dict:
        computational_resource_dict[name] = defaultdict(lambda: 0)
        if profiler == "Deterministic":
            computational_resource_dict[name]["stats"] = ""
    comp = computational_resource_dict[name]
    comp["execution_count"] += 1
    comp["total_time"] += elapsedTime
    if profiler == "Deterministic":
        comp["stats"] += stats
    return 0


def snr_spectrogram(x, x_adv):
    """
    Return the SNR of a batch of samples with spectrogram input

    NOTE: Due to phase effects, this is only an estimate of the SNR.
        For instance, if x[0] = sin(t) and x_adv[0] = sin(t + 2*pi/3),
        Then the SNR will be calculated as infinity, when it should be 1.
        However, the spectrograms will look identical, so as long as the
        model uses spectrograms and not the underlying raw signal,
        this should not have a significant effect on the results.
    """
    if x.shape != x_adv.shape:
        raise ValueError(f"x.shape {x.shape} != x_adv.shape {x_adv.shape}")
    return [float(_snr_spectrogram(x_i, x_adv_i)) for (x_i, x_adv_i) in zip(x, x_adv)]


def snr_spectrogram_db(x, x_adv):
    """
    Return the SNR of a batch of samples with spectrogram input in Decibels (DB)
    """
    return [float(i) for i in 10 * np.log10(snr_spectrogram(x, x_adv))]


SUPPORTED_METRICS = {
    "categorical_accuracy": categorical_accuracy,
    "top_n_categorical_accuracy": top_n_categorical_accuracy,
    "top_5_categorical_accuracy": top_5_categorical_accuracy,
    "norm": norm,
    "l0": l0,
    "l1": l1,
    "l2": l2,
    "lp": lp,
    "linf": linf,
    "snr": snr,
    "snr_db": snr_db,
    "snr_spectrogram": snr_spectrogram,
    "snr_spectrogram_db": snr_spectrogram_db,
}


class MetricList:
    """
    Keeps track of all results from a single metric
    """

    def __init__(self, name, function=None):
        if function is None:
            try:
                self.function = SUPPORTED_METRICS[name]
            except KeyError:
                raise KeyError(f"{name} is not part of armory.utils.metrics")
        elif callable(function):
            self.function = function
        else:
            raise ValueError(f"function must be callable or None, not {function}")
        self.name = name
        self._values = []

    def clear(self):
        self._values.clear()

    def append(self, *args, **kwargs):
        value = self.function(*args, **kwargs)
        self._values.extend(value)

    def __iter__(self):
        return self._values.__iter__()

    def __len__(self):
        return len(self._values)

    def values(self):
        return list(self._values)

    def mean(self):
        return sum(float(x) for x in self._values) / len(self._values)


class MetricsLogger:
    """
    Uses the set of task and perturbation metrics given to it.
    """

    def __init__(
        self,
        task=None,
        perturbation=None,
        means=True,
        record_metric_per_sample=False,
        profiler_type=None,
        computational_resource_dict=None,
    ):
        """
        task - single metric or list of metrics
        perturbation - single metric or list of metrics
        means - whether to return the mean value for each metric
        record_metric_per_sample - whether to return metric values for each sample
        """
        self.tasks = self._generate_counters(task)
        self.adversarial_tasks = self._generate_counters(task)
        self.perturbations = self._generate_counters(perturbation)
        self.means = bool(means)
        self.full = bool(record_metric_per_sample)
        self.computational_resource_dict = {}
        if not self.means and not self.full:
            logger.warning(
                "No metric results will be produced. "
                "To change this, set 'means' or 'record_metric_per_sample' to True."
            )
        if not self.tasks and not self.perturbations:
            logger.warning(
                "No metric results will be produced. "
                "To change this, set one or more 'task' or 'perturbation' metrics"
            )

    def _generate_counters(self, names):
        if names is None:
            names = []
        elif isinstance(names, str):
            names = [names]
        elif not isinstance(names, list):
            raise ValueError(
                f"{names} must be one of (None, str, list), not {type(names)}"
            )
        return [MetricList(x) for x in names]

    @classmethod
    def from_config(cls, config):
        return cls(**config)

    def clear(self):
        for metric in self.tasks + self.adversarial_tasks + self.perturbations:
            metric.clear()

    def update_task(self, y, y_pred, adversarial=False):
        tasks = self.adversarial_tasks if adversarial else self.tasks
        for metric in tasks:
            metric.append(y, y_pred)

    def update_perturbation(self, x, x_adv):
        for metric in self.perturbations:
            metric.append(x, x_adv)

    def log_task(self, adversarial=False, targeted=False):
        if adversarial:
            metrics = self.adversarial_tasks
            task_type = "adversarial"
        else:
            metrics = self.tasks
            task_type = "benign"
        if targeted:
            if adversarial:
                task_type = "targeted " + task_type
            else:
                raise ValueError("benign task cannot be targeted")

        for metric in metrics:
            logger.info(
                f"Average {metric.name} on {task_type} test examples: "
                f"{metric.mean():.2%}"
            )

    def results(self):
        """
        Return dict of results
        """
        results = {}
        for metrics, prefix in [
            (self.tasks, "benign"),
            (self.adversarial_tasks, "adversarial"),
            (self.perturbations, "perturbation"),
        ]:
            for metric in metrics:
                if self.full:
                    results[f"{prefix}_{metric.name}"] = metric.values()
                if self.means:
                    try:
                        results[f"{prefix}_mean_{metric.name}"] = metric.mean()
                    except ZeroDivisionError:
                        raise ZeroDivisionError(
                            f"No values to calculate mean in {prefix}_{metric.name}"
                        )

        for name in self.computational_resource_dict:
            entry = self.computational_resource_dict[name]
            if "execution_count" not in entry or "total_time" not in entry:
                raise ValueError(
                    "Computational resource dictionary entry corrupted, missing data."
                )
            total_time = entry["total_time"]
            execution_count = entry["execution_count"]
            average_time = total_time / execution_count
            results[
                f"Avg. CPU time (s) for {execution_count} executions of {name}"
            ] = average_time
            if "stats" in entry:
                results[f"{name} profiler stats"] = entry["stats"]
        return results
