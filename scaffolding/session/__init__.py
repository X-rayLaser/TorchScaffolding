import os
import json
from collections import defaultdict
from random import shuffle
import csv

import torch

from . import parse
from .parse import get_dataset


def create_session(spec):
    session = Session()
    initializer = SessionInitializer()
    initializer(session, spec)
    return session


def create_and_save_session(spec, session_dir):
    session = create_session(spec)
    saver = SessionSaver(session_dir)
    saver.initial_save(session, spec)


def load_json(path):
    with open(path, encoding='utf-8') as f:
        s = f.read()

    return json.loads(s)


def save_as_json(d, save_path):
    with open(save_path, 'w', encoding='utf-8') as f:
        f.write(json.dumps(d))


class ProgressBar:
    def __init__(self, stage_id, epochs_done, completed):
        self.stage_id = stage_id
        self.epochs_done = epochs_done
        self.completed = completed

    def asdict(self):
        return self.__dict__


class Progress:
    def __init__(self, progress_bars):
        self.progress_bars = progress_bars

    @property
    def epochs_done_total(self):
        return sum(bar.epochs_done for bar in self.progress_bars)

    def increment_progress(self):
        stage_id = self.get_current_stage_id()
        self.progress_bars[stage_id].epochs_done += 1
        self.progress_bars[stage_id].completed = False

    def mark_completed(self):
        stage_id = self.get_current_stage_id()
        self.progress_bars[stage_id].completed = True

    def get_current_stage_id(self):
        ids = [idx for idx, bar in enumerate(self.progress_bars) if not bar.completed]
        if ids:
            return ids[0]

        raise StopTrainingError('All stages are completed')

    def __getitem__(self, idx):
        return self.progress_bars[idx]

    def to_list(self):
        return [bar.asdict() for bar in self.progress_bars]

    def from_list(self, items):
        self.progress_bars = [ProgressBar(**d) for d in items]


class StopTrainingError(Exception):
    pass


# todo: rename to state view
class State:
    def __init__(self, models, optimizers):
        self.models = models
        self.optimizers = optimizers


class Session:
    def __init__(self):
        self.datasets = {}
        self.splits = {}
        self.preprocessors = {}
        self.collators = {}
        self.models = {}
        self.neural_maps = {}
        self.optimizers = {}
        self.batch_adapters = {}
        self.losses = {}
        self.metrics = {}
        self.pipelines = {}

        self.stages = []

        # tracks progress
        # todo: store stage number
        self.progress = Progress([])

    def initialize_state(self):
        return State(models=self.models, optimizers=self.optimizers)


class SessionSaver:
    save_attrs = 'datasets splits preprocessors collators batch_adapters losses metrics'.split(' ')

    def __init__(self, session_dir):
        self.session_dir = session_dir

        self.spec_path = os.path.join(session_dir, 'spec.json')
        self.static_dir = os.path.join(session_dir, 'static')
        self.checkpoints_dir = os.path.join(session_dir, 'checkpoints')
        self.history_dir = os.path.join(session_dir, 'metrics')
        self.extra_path = os.path.join(self.static_dir, 'extra_params.json')

    def initial_save(self, session, spec):
        os.makedirs(self.session_dir)
        os.makedirs(self.static_dir)
        os.makedirs(self.checkpoints_dir)
        os.makedirs(self.history_dir)

        self._save_spec(spec)
        self._save_static(session)
        self.save_checkpoint(session)

    def load_from_latest_checkpoint(self):
        spec = load_json(self.spec_path)

        session = Session()
        restorer = SessionRestorer(self.static_dir)
        restorer(session, spec)

        checkpoint_dirs = os.listdir(self.checkpoints_dir)
        latest_epoch = max(map(int, checkpoint_dirs))
        self.load_checkpoint(session, name=str(latest_epoch))
        return session

    def _save_spec(self, spec):
        save_as_json(spec, self.spec_path)

    def _save_static(self, session):
        object_persistence = ObjectPersistence(self.static_dir)

        for attr in self.save_attrs:
            section = getattr(session, attr)
            for name, instance in section.items():
                object_persistence.save(instance, name)

    def save_checkpoint(self, session):
        state_dir = self.checkpoints_dir
        # todo: map locations when needed
        state = session.initialize_state()

        checkpoint_dir = os.path.join(state_dir, str(session.progress.epochs_done_total))
        os.makedirs(checkpoint_dir)
        save_path = os.path.join(checkpoint_dir, 'checkpoint.pt')

        models_dict = {name: model.state_dict() for name, model in state.models.items()}

        optimizers_dict = {name: optimizer.state_dict() for name, optimizer in state.optimizers.items()}

        torch.save({
            'models': models_dict,
            'optimizers': optimizers_dict,
            'progress': session.progress.to_list()
        }, save_path)

    def load_checkpoint(self, session, name):
        state_dir = self.checkpoints_dir
        state = session.initialize_state()

        checkpoint_dir = os.path.join(state_dir, name)
        state_path = os.path.join(checkpoint_dir, 'checkpoint.pt')

        checkpoint = torch.load(state_path)

        for name, model_state in checkpoint['models'].items():
            state.models[name].load_state_dict(model_state)

        for name, optimizer_state in checkpoint['optimizers'].items():
            state.optimizers[name].load_state_dict(optimizer_state)

        session.progress.from_list(checkpoint["progress"])

    def log_metrics(self, stage_number, epoch, train_metrics, val_metrics):
        # todo: log metrics to csv file
        history_path = os.path.join(self.history_dir, f'{stage_number}.csv')
        history = TrainingHistory(history_path)
        history.add_entry(epoch, train_metrics, val_metrics)


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

        field_names = list(row_dict.keys())

        if not os.path.exists(self.file_path):
            self.create(self.file_path, field_names)

        with open(self.file_path, 'a', encoding='utf-8', newline='') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=field_names)
            writer.writerow(row_dict)

    def scalar(self, t):
        return t.item() if hasattr(t, 'item') else t

    @classmethod
    def create(cls, path, field_names):
        with open(path, 'w', encoding='utf-8', newline='') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(field_names)
        return cls(path)


class ObjectInstaller:
    def setup(self, session, instance, spec=None, **kwargs):
        pass


class PreProcessorInstaller(ObjectInstaller):
    def setup(self, session, instance, spec=None, **kwargs):
        dataset_name = spec["fit"]
        dataset = get_dataset(session, dataset_name)
        instance.fit(dataset)


class SplitterInstaller(ObjectInstaller):
    def setup(self, session, instance, spec=None, **kwargs):
        ds_name = spec["dataset_name"]
        ds = session.datasets[ds_name]

        shuffled_indices = list(range(len(ds)))
        shuffle(shuffled_indices)
        instance.configure(shuffled_indices)


class SessionInitializer:
    group_to_loader = {
        'datasets': parse.DatasetLoader(),
        'splits': parse.SplitLoader(),
        'preprocessors': parse.PreProcessorLoader(),
        'collators': parse.Loader(),
        'models': parse.Loader(),
        'neural_maps': parse.NeuralMapLoader(),
        'optimizers': parse.OptimizerLoader(),
        'batch_adapters': parse.Loader(),
        'losses': parse.LossLoader(),
        'metrics': parse.MetricLoader()
    }

    installers = defaultdict(ObjectInstaller, {
        'preprocessors': PreProcessorInstaller(),
        'splits': SplitterInstaller()
    })

    def __call__(self, session, spec):
        init_dict = spec["initialize"]

        definitions = init_dict["definitions"]

        for d in definitions:
            self.build_object(session, d)

        self.prepare_pipelines(session, init_dict["pipelines"])

        self.load_stages(session, spec)

        self.initialize_progress(session)

    def prepare_pipelines(self, session, spec):
        loader = parse.PipelineLoader()

        pipelines = {}
        for name, pipeline_spec in spec.items():
            pipeline = loader.load(session, pipeline_spec)
            pipelines[name] = pipeline
        session.pipelines = pipelines

    def load_stages(self, session, spec):
        stages_spec = spec["train"]["stages"]
        stage_loader = parse.StageLoader()
        session.stages = [stage_loader.load(session, stage) for stage in stages_spec]

    def initialize_progress(self, session):
        bars = [ProgressBar(idx, 0, completed=False) for idx in range(len(session.stages))]
        session.progress = Progress(bars)

    def build_object(self, session, definition):
        group = definition["group"]
        loader = self.group_to_loader[group]
        installer = self.installers[group]
        name = definition["name"]
        spec = definition["spec"]
        instance = loader.load(session, spec, name)
        installer.setup(session, instance, spec)

        section = getattr(session, group)
        section[name] = instance
        return instance


class SessionRestorer(SessionInitializer):
    installers = defaultdict(ObjectInstaller)

    def __init__(self, static_dir):
        super().__init__()
        self.static_dir = static_dir

    def build_object(self, session, definition):
        instance = super().build_object(session, definition)
        name = definition["name"]
        persistence = ObjectPersistence(self.static_dir)
        persistence.load(instance, name)


class ObjectPersistence:
    def __init__(self, static_dir):
        self.static_dir = static_dir

    def save(self, instance, object_name):
        path = self._build_save_path(object_name)
        serialized_dict = instance.state_dict() if hasattr(instance, 'state_dict') else {}
        save_as_json(serialized_dict, path)

    def load(self, instance, object_name):
        path = self._build_save_path(object_name)

        if not os.path.exists(path):
            return

        object_state = load_json(path)

        if hasattr(instance, 'load_state_dict'):
            instance.load_state_dict(object_state)

    def _build_save_path(self, name):
        return os.path.join(self.static_dir, f'{name}.json')