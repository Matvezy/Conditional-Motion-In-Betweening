"""Utilities and tools for tracking runs with Weights & Biases."""
import logging
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import yaml
from tqdm import tqdm

sys.path.append(str(Path(__file__).parent.parent.parent))  # add utils/ to path
from utils.datasets import LoadImagesAndLabels
from utils.datasets import img2label_paths
from utils.general import colorstr, check_dataset, check_file

try:
    import wandb
    from wandb import init, finish
except ImportError:
    wandb = None

RANK = int(os.getenv('RANK', -1))
WANDB_ARTIFACT_PREFIX = 'wandb-artifact://'


def remove_prefix(from_string, prefix=WANDB_ARTIFACT_PREFIX):
    return from_string[len(prefix):]


def check_wandb_config_file(data_config_file):
    wandb_config = '_wandb.'.join(data_config_file.rsplit('.', 1))  # updated data.yaml path
    if Path(wandb_config).is_file():
        return wandb_config
    return data_config_file


def get_run_info(run_path):
    run_path = Path(remove_prefix(run_path, WANDB_ARTIFACT_PREFIX))
    run_id = run_path.stem
    project = run_path.parent.stem
    entity = run_path.parent.parent.stem
    model_artifact_name = 'run_' + run_id + '_model'
    return entity, project, run_id, model_artifact_name


def check_wandb_resume(opt):
    process_wandb_config_ddp_mode(opt) if RANK not in [-1, 0] else None
    if isinstance(opt.resume, str):
        if opt.resume.startswith(WANDB_ARTIFACT_PREFIX):
            if RANK not in [-1, 0]:  # For resuming DDP runs
                entity, project, run_id, model_artifact_name = get_run_info(opt.resume)
                api = wandb.Api()
                artifact = api.artifact(entity + '/' + project + '/' + model_artifact_name + ':latest')
                modeldir = artifact.download()
                opt.weights = str(Path(modeldir) / "last.pt")
            return True
    return None


def process_wandb_config_ddp_mode(opt):
    with open(check_file(opt.data)) as f:
        data_dict = yaml.safe_load(f)  # data dict
    train_dir, val_dir = None, None
    if isinstance(data_dict['train'], str) and data_dict['train'].startswith(WANDB_ARTIFACT_PREFIX):
        api = wandb.Api()
        train_artifact = api.artifact(remove_prefix(data_dict['train']) + ':' + opt.artifact_alias)
        train_dir = train_artifact.download()
        train_path = Path(train_dir) / 'data/images/'
        data_dict['train'] = str(train_path)

    if isinstance(data_dict['val'], str) and data_dict['val'].startswith(WANDB_ARTIFACT_PREFIX):
        api = wandb.Api()
        val_artifact = api.artifact(remove_prefix(data_dict['val']) + ':' + opt.artifact_alias)
        val_dir = val_artifact.download()
        val_path = Path(val_dir) / 'data/images/'
        data_dict['val'] = str(val_path)
    if train_dir or val_dir:
        ddp_data_path = str(Path(val_dir) / 'wandb_local_data.yaml')
        with open(ddp_data_path, 'w') as f:
            yaml.safe_dump(data_dict, f)
        opt.data = ddp_data_path


class WandbLogger():
    """Log training runs, datasets, models, and predictions to Weights & Biases.

    This logger sends information to W&B at wandb.ai. By default, this information
    includes hyperparameters, system configuration and metrics, model metrics,
    and basic data metrics and analyses.

    By providing additional command line arguments to train.py, datasets,
    models and predictions can also be logged.
    """

    def __init__(self, opt, name, run_id, job_type='Training'):
        # Pre-training routine --
        self.job_type = job_type
        self.wandb, self.wandb_run = wandb, None if not wandb else wandb.run
        self.val_artifact, self.train_artifact = None, None
        self.train_artifact_path, self.val_artifact_path = None, None
        self.result_artifact = None
        self.val_table, self.result_table = None, None
        self.val_table_path_map = None
        self.max_imgs_to_log = 16 
        # It's more elegant to stick to 1 wandb.init call, but useful config data is overwritten in the WandbLogger's wandb.init call
        if isinstance(opt.resume, str):  # checks resume from artifact
            if opt.resume.startswith(WANDB_ARTIFACT_PREFIX):
                entity, project, run_id, model_artifact_name = get_run_info(opt.resume)
                model_artifact_name = WANDB_ARTIFACT_PREFIX + model_artifact_name
                assert wandb, 'install wandb to resume wandb runs'
                # Resume wandb-artifact:// runs here| workaround for not overwriting wandb.config
                self.wandb_run = wandb.init(id=run_id,
                                            project=project,
                                            entity=entity,
                                            resume='allow',
                                            allow_val_change=True)
                opt.resume = model_artifact_name
        elif self.wandb:
            self.wandb_run = wandb.init(config=opt,
                                        resume="allow",
                                        project='RMIB-InfoGAN' if opt.project == 'runs/train' else Path(opt.project).stem,
                                        entity=opt.entity,
                                        name=name,
                                        job_type=job_type,
                                        id=run_id,
                                        allow_val_change=True) if not wandb.run else wandb.run
        if self.wandb_run:
            if self.job_type == 'Training':
                if not opt.resume:
                    wandb_data_dict = self.check_and_upload_dataset(opt) if opt.upload_dataset else data_dict
                    # Info useful for resuming from artifacts
                    self.wandb_run.config.update({'opt': vars(opt), 'data_dict': wandb_data_dict}, allow_val_change=True)
                self.data_dict = self.setup_training(opt, data_dict)
            if self.job_type == 'Dataset Creation':
                self.data_dict = self.check_and_upload_dataset(opt)
        else:
            prefix = colorstr('wandb: ')
            print(f"{prefix}Install Weights & Biases for YOLOv5 logging with 'pip install wandb' (recommended)")

    def check_and_upload_dataset(self, opt):
        assert wandb, 'Install wandb to upload dataset'
        config_path = self.log_dataset_artifact(check_file(opt.data),
                                                opt.single_cls,
                                                '' if opt.project == 'runs/train' else Path(opt.project).stem)
        print("Created dataset config file ", config_path)
        with open(config_path) as f:
            wandb_data_dict = yaml.safe_load(f)
        return wandb_data_dict

    def setup_training(self, opt, data_dict):
        self.log_dict, self.current_epoch = {}, 0
        self.bbox_interval = opt.bbox_interval
        if isinstance(opt.resume, str):
            modeldir, _ = self.download_model_artifact(opt)
            if modeldir:
                self.weights = Path(modeldir) / "last.pt"
                config = self.wandb_run.config
                opt.weights, opt.save_period, opt.batch_size, opt.bbox_interval, opt.epochs, opt.hyp = str(
                    self.weights), config.save_period, config.batch_size, config.bbox_interval, config.epochs, \
                                                                                                       config.opt['hyp']
            data_dict = dict(self.wandb_run.config.data_dict)  # eliminates the need for config file to resume
        if self.val_artifact is None:  # If --upload_dataset is set, use the existing artifact, don't download
            self.train_artifact_path, self.train_artifact = self.download_dataset_artifact(data_dict.get('train'),
                                                                                           opt.artifact_alias)
            self.val_artifact_path, self.val_artifact = self.download_dataset_artifact(data_dict.get('val'),
                                                                                       opt.artifact_alias)
            
        if self.train_artifact_path is not None:
            train_path = Path(self.train_artifact_path) / 'data/images/'
            data_dict['train'] = str(train_path)
        if self.val_artifact_path is not None:
            val_path = Path(self.val_artifact_path) / 'data/images/'
            data_dict['val'] = str(val_path)


        if self.val_artifact is not None:
            self.result_artifact = wandb.Artifact("run_" + wandb.run.id + "_progress", "evaluation")
            self.result_table = wandb.Table(["epoch", "id", "ground truth", "prediction", "avg_confidence"])
            self.val_table = self.val_artifact.get("val")
            if self.val_table_path_map is None:
                self.map_val_table_path()
            wandb.log({"validation dataset": self.val_table})
        if opt.bbox_interval == -1:
            self.bbox_interval = opt.bbox_interval = (opt.epochs // 10) if opt.epochs > 10 else 1
        return data_dict

    def download_dataset_artifact(self, path, alias):
        if isinstance(path, str) and path.startswith(WANDB_ARTIFACT_PREFIX):
            artifact_path = Path(remove_prefix(path, WANDB_ARTIFACT_PREFIX) + ":" + alias)
            dataset_artifact = wandb.use_artifact(artifact_path.as_posix().replace("\\", "/"))
            assert dataset_artifact is not None, "'Error: W&B dataset artifact doesn\'t exist'"
            datadir = dataset_artifact.download()
            return datadir, dataset_artifact
        return None, None

    def download_model_artifact(self, opt):
        if opt.resume.startswith(WANDB_ARTIFACT_PREFIX):
            model_artifact = wandb.use_artifact(remove_prefix(opt.resume, WANDB_ARTIFACT_PREFIX) + ":latest")
            assert model_artifact is not None, 'Error: W&B model artifact doesn\'t exist'
            modeldir = model_artifact.download()
            epochs_trained = model_artifact.metadata.get('epochs_trained')
            total_epochs = model_artifact.metadata.get('total_epochs')
            is_finished = total_epochs is None
            assert not is_finished, 'training is finished, can only resume incomplete runs.'
            return modeldir, model_artifact
        return None, None

    def log_model(self, path, save_period, project, epoch, total_epochs):
        model_artifact = wandb.Artifact('run_' + wandb.run.id + '_model', type='model', metadata={
            'original_url': str(path),
            'epochs_trained': epoch + 1,
            'save period': save_period,
            'project': project,
            'total_epochs': total_epochs,
        })
        wandb.log_artifact(model_artifact,
                           aliases=['epoch ' + str(self.current_epoch)])
        print("Saving model artifact on epoch ", epoch + 1)
        
    def log(self, log_dict):
        if self.wandb_run:
            for key, value in log_dict.items():
                self.log_dict[key] = value

    def end_epoch(self, best_result=False):
        if self.wandb_run:
            with all_logging_disabled():
                wandb.log(self.log_dict)
                self.log_dict = {}
                self.bbox_media_panel_images = []
            if self.result_artifact:
                self.result_artifact.add(self.result_table, 'result')
                wandb.log_artifact(self.result_artifact, aliases=['epoch ' + str(self.current_epoch)])
                wandb.log({"evaluation": self.result_table})
                self.result_table = wandb.Table(["epoch", "id", "ground truth", "prediction", ""])
                self.result_artifact = wandb.Artifact("run_" + wandb.run.id + "_progress", "evaluation")

    def finish_run(self):
        if self.wandb_run:
            if self.log_dict:
                with all_logging_disabled():
                    wandb.log(self.log_dict)
            wandb.run.finish()


@contextmanager
def all_logging_disabled(highest_level=logging.CRITICAL):
    """ source - https://gist.github.com/simon-weber/7853144
    A context manager that will prevent any logging messages triggered during the body from being processed.
    :param highest_level: the maximum logging level in use.
      This would only need to be changed if a custom level greater than CRITICAL is defined.
    """
    previous_level = logging.root.manager.disable
    logging.disable(highest_level)
    try:
        yield
    finally:
        logging.disable(previous_level)
