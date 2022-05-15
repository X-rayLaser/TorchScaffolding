import torch
from .utils import save_session


def train(train_pipeline, train_loader, test_loader, loss_fn, metrics,
          epochs=2, checkpoints_dir=None, stat_ivl=50):
    if 'loss' in metrics:
        metrics['loss'] = loss_fn

    for epoch in range(epochs):
        running_loss = MovingAverage()
        running_metrics = {name: MovingAverage() for name in metrics}

        for i, batch in enumerate(train_loader):
            loss, outputs = train_on_batch(train_pipeline, batch, loss_fn)

            running_loss.update(loss.item())
            update_running_metrics(running_metrics, metrics, outputs, batch["targets"])

            if i % stat_ivl == stat_ivl - 1:
                stat_info = f'\rEpoch {epoch + 1}, iteration {i + 1:5d},'
                if 'loss' in metrics:
                    stat_info += f' loss: {running_loss.value:.3f}'

                for metric_name, metric_avg in running_metrics.items():
                    stat_info += f'; {metric_name}: {metric_avg.value:.3f}'
                    metric_avg.reset()

                print(stat_info, end='')

                running_loss.reset()

            if i // stat_ivl > 5:
                break

        computed_metrics = evaluate(train_pipeline, test_loader, metrics, num_batches=32)
        s = f'\rEpoch {epoch + 1}, '
        for name, value in computed_metrics.items():
            s += f'val {name}: {value}; '

        print(s)
        save_session(train_pipeline, epoch, checkpoints_dir)
        print(f'Saved model to {checkpoints_dir}')


def train_on_batch(train_pipeline, batch, loss_fn):
    for pipe in train_pipeline:
        pipe.optimizer.zero_grad()

    outputs = do_forward_pass(train_pipeline, batch)

    criterion, loss_args, transform = loss_fn

    lookup_table = batch["targets"].copy()
    lookup_table.update(outputs)
    args = [lookup_table[arg_name] for arg_name in loss_args]

    args = transform(*args)
    loss = criterion(*args)

    loss.backward()

    for pipe in train_pipeline:
        pipe.optimizer.step()

    return loss, outputs


def do_forward_pass(train_pipeline, batch):
    inputs = batch["inputs"]

    all_outputs = {}
    for pipe in train_pipeline:
        args = get_dependencies(pipe, inputs, all_outputs)
        outputs = pipe.net(*args)
        all_outputs.update(
            dict(zip(pipe.outputs, outputs))
        )
    return all_outputs


def get_dependencies(pipe, batch_inputs, prev_outputs):
    lookup_table = batch_inputs[pipe.name].copy()
    lookup_table.update(prev_outputs)
    return [lookup_table[var_name] for var_name in pipe.inputs]


def evaluate(val_pipeline, dataloader, metrics, num_batches):
    moving_averages = {metric_name: MovingAverage() for metric_name in metrics}

    with torch.no_grad():
        for i, batch in enumerate(dataloader):
            if i >= num_batches:
                break

            all_outputs = do_forward_pass(val_pipeline, batch)
            update_running_metrics(moving_averages, metrics, all_outputs, batch["targets"])

    return {metric_name: avg.value for metric_name, avg in moving_averages.items()}


def update_running_metrics(moving_averages, metrics, outputs, targets):
    lookup_table = targets.copy()
    lookup_table.update(outputs)

    for metric_name, (metric_fn, metric_args, transform_fn) in metrics.items():
        arg_values = [lookup_table[arg] for arg in metric_args]
        arg_values = transform_fn(*arg_values)
        metric = metric_fn(*arg_values)
        moving_averages[metric_name].update(metric)


# todo: support exponentially weighted averages too
class MovingAverage:
    def __init__(self):
        self.x = 0
        self.num_updates = 0

    def reset(self):
        self.x = 0
        self.num_updates = 0

    def update(self, x):
        self.x += x
        self.num_updates += 1

    @property
    def value(self):
        return self.x / self.num_updates
