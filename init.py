import argparse
import json
import os
import csv

import torch

from scaffolding.training import train, PredictionPipeline
from scaffolding import parse
from scaffolding.utils import load_session, save_data_pipeline, load_data_pipeline, change_model_device, \
    instantiate_class, save_session, load_session_from_last_epoch
from scaffolding.adapters import DefaultAdapter


def load_config(path):
    with open(path) as f:
        s = f.read()

    return json.loads(s)


def parse_adapter(batch_adapter_config):
    def instantiate_fn(class_name, *args, **kwargs):
        args = (model,) + args
        return instantiate_class(class_name, *args, *kwargs)

    if batch_adapter_config:
        return parse.build_generic_serializable_instance(batch_adapter_config)
    else:
        # todo: default adapter
        pass


class TrainingSession:
    def __init__(self, path):
        self.path = path

        self.checkpoints_dir = os.path.join(path, 'checkpoints')
        self.history_path = os.path.join(path, 'history.csv')

        self.data_pipeline_path = os.path.join(path, 'data_pipeline.json')
        self.batch_adapter_path = os.path.join(path, 'batch_adapter.json')
        self.extra_params_path = os.path.join(path, 'extra_params.json')

        self.data_pipeline = load_data_pipeline(self.data_pipeline_path)

        d = load_json(self.batch_adapter_path)
        self.batch_adapter = parse.GenericSerializableInstance.from_dict(d)

        self.extra_params = load_json(self.extra_params_path)
        self.device = torch.device(self.extra_params["device"])
        self.num_epochs = self.extra_params["num_epochs"]

        metrics_dict = self.extra_params.get("metrics", {})
        self.metrics = parse.parse_metrics(metrics_dict, self.data_pipeline, self.device)

        loss_config = self.extra_params.get("loss", {})
        self.criterion = parse.parse_loss(loss_config, self.device)

    @property
    def epochs_trained(self):
        _, dir_names, _ = next(os.walk(self.checkpoints_dir))
        # first checkpoint is for untrained model, therefore we subtract it
        return len(dir_names) - 1

    def restore_from_last_checkpoint(self, inference_mode=False):
        model = load_session_from_last_epoch(self.checkpoints_dir, self.device, inference_mode)
        change_model_device(model, self.data_pipeline.device_str)

        train_pipeline = PredictionPipeline(model, self.device, self.batch_adapter)

        return train_pipeline

    def make_checkpoint(self, train_pipeline, epoch):
        save_session(train_pipeline, epoch, self.checkpoints_dir)

    def log_metrics(self, epoch, train_metrics, val_metrics):
        # todo: log metrics to csv file
        history = TrainingHistory(self.history_path)
        history.add_entry(epoch, train_metrics, val_metrics)

    @classmethod
    def create_session(cls, config, save_path):
        checkpoints_dir = os.path.join(save_path, 'checkpoints')
        history_path = os.path.join(save_path, 'history.csv')
        data_pipeline_path = os.path.join(save_path, 'data_pipeline.json')
        batch_adapter_path = os.path.join(save_path, 'batch_adapter.json')
        extra_params_path = os.path.join(save_path, 'extra_params.json')

        os.makedirs(checkpoints_dir, exist_ok=True)

        epochs = parse.parse_epochs(config)

        data_pipeline = parse.DataPipeline.create(config)
        save_data_pipeline(data_pipeline, data_pipeline_path)

        model = parse.parse_model(config)
        change_model_device(model, data_pipeline.device_str)
        save_session(model, 0, checkpoints_dir)

        batch_adapter = parse_adapter(config["training"].get("batch_adapter"))
        save_as_json(batch_adapter.to_dict(), batch_adapter_path)

        extra_params = {}

        training_config = config["training"]

        extra_params["device"] = training_config.get("device", "cpu")

        if "loss" in training_config:
            extra_params["loss"] = training_config["loss"]
        if "metrics" in training_config:
            extra_params["metrics"] = training_config["metrics"]

        extra_params["num_epochs"] = epochs
        save_as_json(extra_params, extra_params_path)

        metrics_dict = extra_params.get("metrics", [])
        metrics_dict.keys()

        field_names = list(metrics_dict.keys()) + [f'val {name}' for name in metrics_dict.keys()]

        history = TrainingHistory.create(history_path, field_names)
        # todo: calculate metrics for 0-th epoch (before any training)


class TrainingHistory:
    def __init__(self, file_path):
        self.file_path = file_path

    def add_entry(self, epoch, train_metrics, val_metrics):
        # todo: make sure the ordering is right
        val_metrics = {f'val {k}': v for k, v in val_metrics.items()}

        all_metrics = {}
        all_metrics.update(train_metrics)
        all_metrics.update(val_metrics)

        row_dict = {'epoch': epoch}
        row_dict.update({k: self.scalar(v) for k, v in all_metrics.items()})

        with open(self.file_path, 'a', encoding='utf-8', newline='') as csvfile:
            fieldnames = list(row_dict.keys())
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writerow(row_dict)

    def scalar(self, t):
        return t.item() if hasattr(t, 'item') else t

    @classmethod
    def create(cls, path, field_names):
        with open(path, 'w', encoding='utf-8', newline='') as csvfile:
            writer = csv.writer(csvfile)
            row = ['Epoch #'] + field_names
            writer.writerow(row)
        return cls(path)


def load_json(path):
    with open(path, encoding='utf-8') as f:
        s = f.read()

    return json.loads(s)


def save_as_json(d, save_path):
    with open(save_path, 'w', encoding='utf-8') as f:
        f.write(json.dumps(d))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Train ML pipeline according to a specified configuration file'
    )
    parser.add_argument('config', type=str, help='Path to the configuration file')

    cmd_args = parser.parse_args()
    path = cmd_args.config

    config = load_config(path)
    config = config["pipeline"]

    session_dir = parse.parse_checkpoint_dir(config)

    if os.path.exists(session_dir):
        print(f"Session already exists under {session_dir}")
    else:
        TrainingSession.create_session(config, session_dir)
