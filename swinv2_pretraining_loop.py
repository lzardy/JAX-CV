import argparse
import json
from datetime import datetime
from typing import Any, Callable, Union

import jax
import jax.numpy as jnp
import optax
import orbax.checkpoint
import tensorflow as tf
from clu import metrics
from flax import jax_utils, struct
from flax.training import orbax_utils, train_state
from tqdm import tqdm

import Models
from Generators.SimMIMGen import DataGenerator


@struct.dataclass
class Metrics(metrics.Collection):
    loss: metrics.Metric


class TrainState(train_state.TrainState):
    metrics: Metrics
    constants: Any
    pt_constants: Any


def create_train_state(
    module,
    params_key,
    target_size: int,
    mask_input_size: int,
    num_classes: int,
    learning_rate: Union[float, Callable],
    weight_decay: float,
):
    """Creates an initial 'TrainState'."""
    # initialize parameters by passing a template image
    variables = module.init(
        params_key,
        jnp.ones([1, target_size, target_size, 3]),
        mask=jnp.ones([1, mask_input_size, mask_input_size]),
        train=False,
    )
    params = variables["params"]
    constants = variables["swinv2_constants"]
    pt_constants = variables["simmim_constants"]

    loss = metrics.Average.from_output("loss")
    collection = Metrics.create(loss=loss)

    def should_decay(path, _):
        is_kernel = path[-1].key == "kernel"
        is_cpb = "attention_bias" in [x.key for x in path]
        return is_kernel and not is_cpb

    wd_mask = jax.tree_util.tree_map_with_path(should_decay, params)
    tx = optax.lamb(learning_rate, weight_decay=weight_decay, mask=wd_mask)
    return TrainState.create(
        apply_fn=module.apply,
        params=params,
        tx=tx,
        metrics=collection.empty(),
        constants=constants,
        pt_constants=pt_constants,
    )


def train_step(state, batch, dropout_key):
    """Train for a single step."""
    dropout_train_key = jax.random.fold_in(key=dropout_key, data=state.step)

    def loss_fn(params, constants, pt_constants):
        loss, _ = state.apply_fn(
            {
                "params": params,
                "swinv2_constants": constants,
                "simmim_constants": pt_constants,
            },
            batch["images"],
            mask=batch["masks"],
            train=True,
            rngs={"dropout": dropout_train_key},
        )
        return loss

    grad_fn = jax.value_and_grad(loss_fn)
    loss, grads = grad_fn(state.params, state.constants, state.pt_constants)
    grads = jax.lax.pmean(grads, axis_name="batch")
    state = state.apply_gradients(grads=grads)

    metric_updates = state.metrics.gather_from_model_output(loss=loss)
    metrics = state.metrics.merge(metric_updates)
    state = state.replace(metrics=metrics)
    return state


def eval_step(*, state, batch):
    loss, _ = state.apply_fn(
        {
            "params": state.params,
            "swinv2_constants": state.constants,
            "simmim_constants": state.pt_constants,
        },
        batch["images"],
        mask=batch["masks"],
        train=False,
    )

    metric_updates = state.metrics.gather_from_model_output(loss=loss)
    metrics = state.metrics.merge(metric_updates)
    state = state.replace(metrics=metrics)
    return state


parser = argparse.ArgumentParser(description="Train a network")
parser.add_argument(
    "--model-name",
    default="simmim_swinv2_tiny",
    help="Model variant to train",
    type=str,
)
parser.add_argument(
    "--run-name",
    default=None,
    help="Run name. If left empty it gets autogenerated",
    type=str,
)
parser.add_argument(
    "--restore-params-ckpt",
    default=None,
    help="Restore the parameters from the last step of the given orbax checkpoint. Must be an absolute path. WARNING: restores params only!",
    type=str,
)
parser.add_argument(
    "--dataset-file",
    default="datasets/aibooru.json",
    help="JSON file with dataset specs",
    type=str,
)
parser.add_argument(
    "--dataset-root",
    default="/home/smilingwolf/datasets",
    help="Dataset root, where the record_shards_train and record_shards_val folders are stored",
    type=str,
)
parser.add_argument(
    "--checkpoints-root",
    default="/mnt/c/Users/SmilingWolf/Desktop/TFKeras/JAX/checkpoints",
    help="Checkpoints root, where the checkpoints will be stored following a <ckpt_root>/<run_name>/<epoch> structure",
    type=str,
)
parser.add_argument(
    "--epochs",
    default=50,
    help="Number of epochs to train for",
    type=int,
)
parser.add_argument(
    "--warmup-epochs",
    default=5,
    help="Number of epochs to dedicate to linear warmup",
    type=int,
)
parser.add_argument(
    "--batch-size",
    default=64,
    help="Per-device batch size",
    type=int,
)
parser.add_argument(
    "--image-size",
    default=256,
    help="Image resolution in input to the network",
    type=int,
)
parser.add_argument(
    "--learning-rate",
    default=0.001,
    help="Max learning rate",
    type=float,
)
parser.add_argument(
    "--weight-decay",
    default=0.0001,
    help="Weight decay",
    type=float,
)
parser.add_argument(
    "--dropout-rate",
    default=0.1,
    help="Stochastic depth rate",
    type=float,
)
parser.add_argument(
    "--mixup-alpha",
    default=0.8,
    help="MixUp alpha",
    type=float,
)
parser.add_argument(
    "--rotation-ratio",
    default=0.0,
    help="Rotation ratio as a fraction of PI",
    type=float,
)
parser.add_argument(
    "--cutout-max-pct",
    default=0.1,
    help="Cutout area as a fraction of the total image area",
    type=float,
)
parser.add_argument(
    "--cutout-patches",
    default=1,
    help="Number of cutout patches",
    type=int,
)
args = parser.parse_args()

model_name = args.model_name
model_builder = Models.model_registry[model_name]

run_name = args.run_name
if run_name is None:
    now = datetime.now()
    date_time = now.strftime("%Y_%m_%d_%Hh%Mm%Ss")
    run_name = f"{model_name}_{date_time}"

checkpoints_root = args.checkpoints_root
dataset_root = args.dataset_root
with open(args.dataset_file) as f:
    dataset_specs = json.load(f)

# Run params
num_epochs = args.epochs
warmup_epochs = args.warmup_epochs
batch_size = args.batch_size
compute_units = jax.device_count()
global_batch_size = batch_size * compute_units

# Dataset params
image_size = args.image_size
num_classes = 0
train_samples = dataset_specs["train_samples"]
val_samples = dataset_specs["val_samples"]

# Model hyperparams
window_ratio = 32
window_size = image_size // window_ratio
learning_rate = args.learning_rate
weight_decay = args.weight_decay
dropout_rate = args.dropout_rate

# Augmentations hyperparams
noise_level = 2
mixup_alpha = args.mixup_alpha
rotation_ratio = args.rotation_ratio
cutout_max_pct = args.cutout_max_pct
cutout_patches = args.cutout_patches
random_resize_method = True
mask_patch_size = 32
model_patch_size = 4
mask_input_size = image_size // model_patch_size

tf.random.set_seed(0)
root_key = jax.random.key(0)
params_key, dropout_key = jax.random.split(key=root_key, num=2)
dropout_keys = jax.random.split(key=dropout_key, num=jax.device_count())
del root_key, dropout_key

training_generator = DataGenerator(
    f"{dataset_root}/record_shards_train/*",
    num_classes=num_classes,
    image_size=image_size,
    batch_size=batch_size,
    num_devices=compute_units,
    noise_level=noise_level,
    mixup_alpha=mixup_alpha,
    rotation_ratio=rotation_ratio,
    cutout_max_pct=cutout_max_pct,
    cutout_patches=cutout_patches,
    random_resize_method=random_resize_method,
    mask_patch_size=mask_patch_size,
    model_patch_size=model_patch_size,
    mask_ratio=0.6,
)
train_ds = training_generator.genDS()
train_ds = jax_utils.prefetch_to_device(train_ds.as_numpy_iterator(), size=2)

validation_generator = DataGenerator(
    f"{dataset_root}/record_shards_val/*",
    num_classes=num_classes,
    image_size=image_size,
    batch_size=batch_size,
    num_devices=compute_units,
    noise_level=0,
    mixup_alpha=0.0,
    rotation_ratio=0.0,
    cutout_max_pct=0.0,
    random_resize_method=False,
    mask_patch_size=32,
    model_patch_size=4,
    mask_ratio=0.6,
)
val_ds = validation_generator.genDS()
val_ds = jax_utils.prefetch_to_device(val_ds.as_numpy_iterator(), size=2)

model = model_builder(
    img_size=image_size,
    num_classes=num_classes,
    window_size=window_size,
    drop_path_rate=dropout_rate,
    dtype=jnp.bfloat16,
)
# tab_img = jnp.ones([1, image_size, image_size, 3])
# tab_mask = jnp.ones([1, mask_input_size, mask_input_size])
# print(model.tabulate(jax.random.key(0), tab_img, tab_mask, train=False))

num_steps_per_epoch = train_samples // global_batch_size
learning_rate = optax.warmup_cosine_decay_schedule(
    init_value=learning_rate * 0.1,
    peak_value=learning_rate,
    warmup_steps=num_steps_per_epoch * warmup_epochs,
    decay_steps=num_steps_per_epoch * num_epochs,
    end_value=learning_rate * 0.01,
)

state = create_train_state(
    model,
    params_key,
    image_size,
    mask_input_size,
    0,
    learning_rate,
    weight_decay,
)
del params_key

metrics_history = {
    "train_loss": [],
    "train_f1score": [],
    "train_mcc": [],
    "val_loss": [],
    "val_f1score": [],
    "val_mcc": [],
}
ckpt = {"model": state, "metrics_history": metrics_history}

orbax_checkpointer = orbax.checkpoint.PyTreeCheckpointer()
options = orbax.checkpoint.CheckpointManagerOptions(
    max_to_keep=2,
    best_fn=lambda metrics: metrics["val_loss"],
    best_mode="min",
    create=True,
)
checkpoint_manager = orbax.checkpoint.CheckpointManager(
    f"{checkpoints_root}/{run_name}",
    orbax_checkpointer,
    options,
)

if args.restore_params_ckpt is not None:
    throwaway_manager = orbax.checkpoint.CheckpointManager(
        args.restore_params_ckpt,
        orbax_checkpointer,
    )
    latest_epoch = throwaway_manager.latest_step()
    restored = throwaway_manager.restore(latest_epoch, items=ckpt)
    state = state.replace(params=restored["model"].params)
    del throwaway_manager

latest_epoch = checkpoint_manager.latest_step()
if latest_epoch is not None:
    restored = checkpoint_manager.restore(latest_epoch, items=ckpt)
    state = state.replace(params=restored["model"].params)
    metrics_history = restored["metrics_history"]
else:
    latest_epoch = 0

state = jax_utils.replicate(state)
p_train_step = jax.pmap(train_step, axis_name="batch")
p_eval_step = jax.pmap(eval_step, axis_name="batch")

epochs = 0
pbar = tqdm(total=num_steps_per_epoch)
for step, batch in enumerate(train_ds):
    # Run optimization steps over training batches and compute batch metrics
    # get updated train state (which contains the updated parameters)
    state = p_train_step(state=state, batch=batch, dropout_key=dropout_keys)

    if step % 256 == 0:
        merged_metrics = jax_utils.unreplicate(state.metrics)
        pbar.set_postfix(loss=f"{merged_metrics.loss.compute():.04f}")

    pbar.update(1)

    # one training epoch has passed
    if (step + 1) % num_steps_per_epoch == 0:
        # compute metrics
        merged_metrics = jax_utils.unreplicate(state.metrics)
        for metric, value in merged_metrics.compute().items():
            # record metrics
            metrics_history[f"train_{metric}"].append(value)

        # reset train_metrics for next training epoch
        empty_metrics = state.metrics.empty()
        empty_metrics = jax_utils.replicate(empty_metrics)
        state = state.replace(metrics=empty_metrics)

        # Compute metrics on the validation set after each training epoch
        val_state = state
        for val_step, val_batch in enumerate(val_ds):
            val_state = p_eval_step(state=val_state, batch=val_batch)
            if val_step == val_samples // global_batch_size:
                break

        val_state = jax_utils.unreplicate(val_state)
        for metric, value in val_state.metrics.compute().items():
            metrics_history[f"val_{metric}"].append(value)

        print(
            f"train epoch: {(step+1) // num_steps_per_epoch}, "
            f"loss: {metrics_history['train_loss'][-1]:.04f}"
        )
        print(
            f"val epoch: {(step+1) // num_steps_per_epoch}, "
            f"loss: {metrics_history['val_loss'][-1]:.04f}"
        )

        ckpt["model"] = val_state
        ckpt["metrics_history"] = metrics_history
        save_args = orbax_utils.save_args_from_target(ckpt)
        checkpoint_manager.save(
            epochs + latest_epoch,
            ckpt,
            save_kwargs={"save_args": save_args},
            metrics={"val_loss": float(metrics_history["val_loss"][-1])},
        )

        epochs += 1
        if epochs == num_epochs:
            break

        pbar.reset()

pbar.close()
