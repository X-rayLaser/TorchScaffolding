from collections import namedtuple

import torch
from torch import nn

import torchvision
import torchvision.transforms as transforms
import torch.optim as optim

from scaffolding.metrics import metric_functions
from scaffolding.utils import SimpleSplitter, instantiate_class, import_function, AdaptedCollator, WrappedDataset
from scaffolding.store import store
from scaffolding.exceptions import InvalidParameterError


def parse_transform(transform_dict):
    name = transform_dict["name"]
    args_list = transform_dict.get("args", [])

    if name == "totensor":
        return transforms.ToTensor()
    if name == "normalize":
        return transforms.Normalize(tuple(args_list[0]), tuple(args_list[1]))


def get_transform_pipeline(config_dict):
    transform_config = config_dict["data"].get("transform")
    if transform_config:
        return transforms.Compose([parse_transform(t) for t in transform_config])
    return transforms.Compose([])


def parse_data_pipeline(config_dict):
    train_set, test_set = parse_datasets(config_dict)
    preprocessors = fit_preprocessors(train_set, config_dict)
    train_set = WrappedDataset(train_set, preprocessors)
    test_set = WrappedDataset(test_set, preprocessors)

    collate_fn = parse_collator(config_dict)
    batch_adapter = parse_batch_adapter(config_dict)
    final_collate_fn = AdaptedCollator(collate_fn, batch_adapter)

    batch_size = config_dict["data"]["batch_size"]

    train_loader = torch.utils.data.DataLoader(train_set, batch_size=batch_size,
                                               shuffle=True, num_workers=2, collate_fn=final_collate_fn)

    test_loader = torch.utils.data.DataLoader(test_set, batch_size=batch_size,
                                              shuffle=False, num_workers=2, collate_fn=final_collate_fn)
    return train_loader, test_loader


def parse_datasets(config_dict):
    ds_class_name = config_dict["data"]["dataset_name"]

    if '.' in ds_class_name:
        # assume that we got a fully-qualified path to a custom class
        ds_kwargs = config_dict["data"].get("dataset_kwargs", {})
        dataset = instantiate_class(ds_class_name, **ds_kwargs)
        splitter = SimpleSplitter(dataset)
        train_set = splitter.train_ds
        test_set = splitter.val_ds
    else:
        dataset_class = getattr(torchvision.datasets, ds_class_name)
        transform = get_transform_pipeline(config_dict)
        train_set = dataset_class(root='./data', train=True, download=True, transform=transform)
        test_set = dataset_class(root='./data', train=False, download=True, transform=transform)
    return train_set, test_set


def fit_preprocessors(train_set, config_dict):
    preprocessors_config = config_dict["data"]["preprocessors"]
    preprocessors = []
    for d in preprocessors_config:
        preprocessor = instantiate_class(d["class"])

        preprocessor.fit(train_set)
        preprocessors.append(preprocessor)

        for attr_name in d.get('expose_attributes', []):
            attr_value = getattr(preprocessor, attr_name)
            setattr(store, attr_name, attr_value)
    return preprocessors


def parse_collator(config_dict):
    collator_config = config_dict["data"]["collator"]
    collator_class_name = collator_config["class"]

    collator_args = collator_config.get("args", [])
    collator_kwargs = collator_config.get("kwargs", {})

    dynamic_kwargs = {}
    for collator_arg_name, store_arg_name in collator_config.get("dynamic_kwargs", {}).items():
        dynamic_kwargs[collator_arg_name] = getattr(store, store_arg_name)

    collator_kwargs.update(dynamic_kwargs)
    return instantiate_class(collator_class_name, *collator_args, **collator_kwargs)


def parse_batch_adapter(config_dict):
    adapter_config = config_dict["data"]["batch_adapter"]
    adapter_class_name = adapter_config["class"]
    adapter_args = adapter_config.get("args", [])
    adapter_kwargs = adapter_config.get("kwargs", {})

    dynamic_kwargs = {}
    for adapter_arg_name, store_arg_name in adapter_config.get("dynamic_kwargs", {}).items():
        dynamic_kwargs[adapter_arg_name] = getattr(store, store_arg_name)

    adapter_kwargs.update(dynamic_kwargs)
    return instantiate_class(adapter_class_name, *adapter_args, **adapter_kwargs)


def parse_metrics(config_dict):
    metrics_config = config_dict["training"]["metrics"]
    metrics = {}

    try:
        error = False
        exc_args = None
        metrics = {metric_name: (metric_functions[metric_name], metrics_config[metric_name]["inputs"])
                   for metric_name in metrics_config}
    except KeyError as exc:
        error = True
        exc_args = exc.args

    if error:
        allowed_metrics = list(metric_functions.keys())
        error_message = f'Unknown metric "{exc_args[0]}". Must be one of {allowed_metrics}'
        raise InvalidParameterError(error_message)
    return metrics


def parse_model(config_dict):
    model_config = config_dict["training"]["model"]
    return [parse_submodel(config) for config in model_config]


def parse_submodel(config):
    """Returns a tuple of (nn.Module instance, optimizer, inputs, outputs)"""
    path = config["arch"]
    model_args = config.get("args", [])
    model_kwargs = config.get("kwargs", {})

    dynamic_kwargs = config.get("dynamic_kwargs", {})
    total_kwargs = model_kwargs.copy()

    for k, v in dynamic_kwargs.items():
        total_kwargs[k] = getattr(store, v)

    sub_model = instantiate_class(path, *model_args, **total_kwargs)

    optimizer_config = config["optimizer"]
    optimizer_class_name = optimizer_config["class"]
    optimizer_params = optimizer_config["params"]

    optimizer_class = getattr(optim, optimizer_class_name)
    optimizer = optimizer_class(sub_model.parameters(), **optimizer_params)
    cls = namedtuple('SubModel', ['name', 'net', 'optimizer', 'inputs', 'outputs'])
    return cls(config["name"], sub_model, optimizer, config["inputs"], config["outputs"])


def parse_loss(config_dict):
    loss_config = config_dict["training"]["loss"]
    loss_class_name = loss_config["class"]
    if "transform" in loss_config:
        transform_fn = import_function(loss_config["transform"])
    else:
        transform_fn = lambda *args: args

    criterion_class = getattr(nn, loss_class_name)
    args = loss_config.get("args", [])
    kwargs = loss_config.get("kwargs", {})
    return criterion_class(*args, **kwargs), loss_config["inputs"], transform_fn


def parse_epochs(config_dict):
    return config_dict["training"]["num_epochs"]