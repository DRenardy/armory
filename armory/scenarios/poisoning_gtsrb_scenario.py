"""
Classifier evaluation within ARMORY

Scenario Contributor: MITRE Corporation
"""

import logging
from typing import Optional

import numpy as np
from tensorflow import set_random_seed
from tensorflow.keras.utils import to_categorical
from tqdm import tqdm

from armory.utils.config_loading import (
    load_dataset,
    load_model,
    load,
    load_fn,
)
from armory.utils import metrics
from armory.scenarios.base import Scenario

logger = logging.getLogger(__name__)


def poison_dataset(src_imgs, src_lbls, src, tgt, ds_size, attack, poisoned_indices):
    # In this example, all images of "src" class have a trigger
    # added and re-labeled as "tgt" class
    # NOTE: currently art.attacks.PoisonAttackBackdoor only supports
    #   black-white images.  One way to generate poisoned examples
    #   is to convert each batch of multi-channel images of shape
    #   (N,W,H,C) to N separate (C,W,H)-tuple, where C would be
    #   interpreted by PoisonAttackBackdoor as the batch size,
    #   and each channel would have a backdoor trigger added
    poison_x = []
    poison_y = []
    for idx in range(ds_size):
        if src_lbls[idx] == src and idx in poisoned_indices:
            src_img = np.transpose(src_imgs[idx], (2, 0, 1))
            p_img, p_label = attack.poison(src_img, [tgt])
            poison_x.append(np.transpose(p_img, (1, 2, 0)))
            poison_y.append(p_label)
        else:
            poison_x.append(src_imgs[idx])
            poison_y.append(src_lbls[idx])

    poison_x, poison_y = np.array(poison_x), np.array(poison_y)

    return poison_x, poison_y


class GTSRB(Scenario):
    def _evaluate(self, config: dict, num_eval_batches: Optional[int]) -> dict:
        """
        Evaluate a config file for classification robustness against attack.

        Note: num_eval_batches shouldn't be set for poisoning scenario and will raise an
        error if it is
        """
        if num_eval_batches:
            raise ValueError("num_eval_batches shouldn't be set for poisoning scenario")

        model_config = config["model"]
        # Scenario assumes preprocessing_fn makes images all same size
        classifier, preprocessing_fn = load_model(model_config)

        config_adhoc = config.get("adhoc") or {}
        train_epochs = config_adhoc["train_epochs"]
        src_class = config_adhoc["source_class"]
        tgt_class = config_adhoc["target_class"]
        fit_batch_size = config_adhoc.get(
            "fit_batch_size", config["dataset"]["batch_size"]
        )

        # Set random seed due to large variance in attack and defense success
        np.random.seed(config_adhoc["np_seed"])
        set_random_seed(config_adhoc["tf_seed"])
        use_poison_filtering_defense = config_adhoc.get(
            "use_poison_filtering_defense", True
        )
        if self.check_run:
            # filtering defense requires more than a single batch to run properly
            use_poison_filtering_defense = False

        logger.info(f"Loading dataset {config['dataset']['name']}...")

        clean_data = load_dataset(
            config["dataset"],
            epochs=1,
            split_type="train",
            preprocessing_fn=preprocessing_fn,
            shuffle_files=False,
        )

        attack_config = config["attack"]
        attack_type = attack_config.get("type")

        if attack_type == "preloaded":
            num_images_tgt_class = config_adhoc["num_images_target_class"]
            logger.info(
                f"Loading poison dataset {config_adhoc['poison_samples']['name']}..."
            )
            num_poisoned = int(config_adhoc["fraction_poisoned"] * num_images_tgt_class)
            if num_poisoned == 0:
                raise ValueError(
                    "For the preloaded attack, fraction_poisoned must be set so that at least on data point is poisoned."
                )
            config_adhoc["poison_samples"]["batch_size"] = num_poisoned
            poison_data = load_dataset(
                config["adhoc"]["poison_samples"],
                epochs=1,
                split_type="poison",
                preprocessing_fn=None,
            )
        else:
            attack = load(attack_config)
        logger.info("Building in-memory dataset for poisoning detection and training")
        fraction_poisoned = config["adhoc"]["fraction_poisoned"]
        poison_dataset_flag = config["adhoc"]["poison_dataset"]

        # detect_poison does not currently support data generators
        #     therefore, make in memory dataset
        x_train_all, y_train_all = [], []
        if attack_type == "preloaded":
            for x_clean, y_clean in clean_data:
                x_poison, y_poison = poison_data.get_batch()
                x_poison = np.array([xp for xp in x_poison], dtype=np.float)
                x_train_all.append(x_clean)
                y_train_all.append(y_clean)
                x_train_all.append(x_poison)
                y_train_all.append(y_poison)
            x_train_all = np.concatenate(x_train_all, axis=0)
            y_train_all = np.concatenate(y_train_all, axis=0)
        else:
            for x_train, y_train in clean_data:
                x_train_all.append(x_train)
                y_train_all.append(y_train)
            x_train_all = np.concatenate(x_train_all, axis=0)
            y_train_all = np.concatenate(y_train_all, axis=0)
            if poison_dataset_flag:
                total_count = np.bincount(y_train_all)[src_class]
                poison_count = int(fraction_poisoned * total_count)
                if poison_count == 0:
                    logger.warning(
                        f"No poisons generated with fraction_poisoned {fraction_poisoned} for class {src_class}."
                    )
                src_indices = np.where(y_train_all == src_class)[0]
                poisoned_indices = np.random.choice(
                    src_indices, size=poison_count, replace=False
                )
                x_train_all, y_train_all = poison_dataset(
                    x_train_all,
                    y_train_all,
                    src_class,
                    tgt_class,
                    y_train_all.shape[0],
                    attack,
                    poisoned_indices,
                )

        y_train_all_categorical = to_categorical(y_train_all)

        if use_poison_filtering_defense:
            defense_config = config["defense"]

            defense_model_config = config_adhoc.get("defense_model", model_config)
            defense_train_epochs = config_adhoc.get(
                "defense_train_epochs", train_epochs
            )
            classifier_for_defense, _ = load_model(defense_model_config)
            logger.info(
                f"Fitting model {defense_model_config['module']}.{defense_model_config['name']} "
                f"for defense {defense_config['name']}..."
            )
            classifier_for_defense.fit(
                x_train_all,
                y_train_all_categorical,
                batch_size=fit_batch_size,
                nb_epochs=defense_train_epochs,
                verbose=False,
            )
            defense_fn = load_fn(defense_config)
            defense = defense_fn(
                classifier_for_defense, x_train_all, y_train_all_categorical
            )

            _, is_clean = defense.detect_poison(nb_clusters=2, nb_dims=43, reduce="PCA")
            is_clean = np.array(is_clean)
            logger.info(f"Total clean data points: {np.sum(is_clean)}")

            logger.info("Filtering out detected poisoned samples")
            indices_to_keep = is_clean == 1
            x_train_final = x_train_all[indices_to_keep]
            y_train_final = y_train_all_categorical[indices_to_keep]
        else:
            logger.info(
                "Defense does not require filtering. Model fitting will use all data."
            )
            x_train_final = x_train_all
            y_train_final = y_train_all_categorical
        if len(x_train_final):
            logger.info(
                f"Fitting model of {model_config['module']}.{model_config['name']}..."
            )
            classifier.fit(
                x_train_final,
                y_train_final,
                batch_size=fit_batch_size,
                nb_epochs=train_epochs,
                verbose=False,
            )
        else:
            logger.warning("All data points filtered by defense. Skipping training")

        logger.info("Validating on clean test data")
        config["dataset"]["batch_size"] = fit_batch_size
        test_data = load_dataset(
            config["dataset"],
            epochs=1,
            split_type="test",
            preprocessing_fn=preprocessing_fn,
            shuffle_files=False,
        )
        validation_metric = metrics.MetricList("categorical_accuracy")
        target_class_benign_metric = metrics.MetricList("categorical_accuracy")
        for x, y in tqdm(test_data, desc="Testing"):
            y_pred = classifier.predict(x)
            validation_metric.append(y, y_pred)
            y_pred_tgt_class = y_pred[y == src_class]
            if len(y_pred_tgt_class):
                target_class_benign_metric.append(
                    [src_class] * len(y_pred_tgt_class), y_pred_tgt_class
                )
        logger.info(f"Unpoisoned validation accuracy: {validation_metric.mean():.2%}")
        logger.info(
            f"Unpoisoned validation accuracy on targeted class: {target_class_benign_metric.mean():.2%}"
        )
        results = {
            "validation_accuracy": validation_metric.mean(),
            "validation_accuracy_targeted_class": target_class_benign_metric.mean(),
        }

        test_metric = metrics.MetricList("categorical_accuracy")
        targeted_test_metric = metrics.MetricList("categorical_accuracy")

        logger.info("Testing on poisoned test data")
        if attack_type == "preloaded":
            test_data_poison = load_dataset(
                config_adhoc["poison_samples"],
                epochs=1,
                split_type="poison_test",
                preprocessing_fn=None,
            )
            for x_poison_test, y_poison_test in tqdm(
                test_data_poison, desc="Testing poison"
            ):
                x_poison_test = np.array([xp for xp in x_poison_test], dtype=np.float)
                y_pred = classifier.predict(x_poison_test)
                y_true = [src_class] * len(y_pred)
                targeted_test_metric.append(y_poison_test, y_pred)
                test_metric.append(y_true, y_pred)
            test_data_clean = load_dataset(
                config["dataset"],
                epochs=1,
                split_type="test",
                preprocessing_fn=preprocessing_fn,
                shuffle_files=False,
            )
            for x_clean_test, y_clean_test in tqdm(
                test_data_clean, desc="Testing clean"
            ):
                x_clean_test = np.array([xp for xp in x_clean_test], dtype=np.float)
                y_pred = classifier.predict(x_clean_test)
                test_metric.append(y_clean_test, y_pred)

        elif poison_dataset_flag:
            logger.info("Testing on poisoned test data")
            test_data = load_dataset(
                config["dataset"],
                epochs=1,
                split_type="test",
                preprocessing_fn=preprocessing_fn,
                shuffle_files=False,
            )
            for x_test, y_test in tqdm(test_data, desc="Testing"):
                src_indices = np.where(y_test == src_class)[0]
                poisoned_indices = src_indices  # Poison entire class
                x_test, _ = poison_dataset(
                    x_test,
                    y_test,
                    src_class,
                    tgt_class,
                    len(y_test),
                    attack,
                    poisoned_indices,
                )
                y_pred = classifier.predict(x_test)
                test_metric.append(y_test, y_pred)

                y_pred_targeted = y_pred[y_test == src_class]
                if not len(y_pred_targeted):
                    continue
                targeted_test_metric.append(
                    [tgt_class] * len(y_pred_targeted), y_pred_targeted
                )

        if poison_dataset_flag or attack_type == "preloaded":
            results["test_accuracy"] = test_metric.mean()
            results["targeted_misclassification_accuracy"] = targeted_test_metric.mean()
            logger.info(f"Test accuracy: {test_metric.mean():.2%}")
            logger.info(
                f"Test targeted misclassification accuracy: {targeted_test_metric.mean():.2%}"
            )

        return results
