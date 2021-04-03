import logging
from argparse import Namespace
from datetime import datetime

import click
import torch

from luke.utils.entity_vocab import MASK_TOKEN

from zero.model import Zero
from zero.ner.model import LukeForNamedEntityRecognition
from zero.utils import set_seed
from zero.utils.trainer import Trainer, trainer_args

from zero.utils.evaluator import evaluate
from zero.utils.loader import load_domain_features, load_and_cache_examples, \
    get_saved_paths
from utils_io import *

logger = logging.getLogger(__name__)


def get_exp_name(task_name):
    return "{}-{}".format(task_name, datetime.now().strftime("%D-%H-%M-%S").replace("/", "_"))


@click.group(name="ner")
def cli():
    pass


@cli.command()
@click.option("--log-dir", default="runs", type=click.Path())
@click.option("--task-name", default="zero")
@click.option("--data-dir", default="data", type=click.Path(exists=True))
@click.option("--train-domains", default="science")
@click.option("--dev-domain", default="science")
@click.option("--test-domain", default="music")
@click.option("--max-seq-length", default=512)
@click.option("--max-entity-length", default=128)
@click.option("--max-mention-length", default=25)
@click.option("--no-word-feature", is_flag=True)
@click.option("--no-entity-feature", is_flag=True)
@click.option("--do-train/--no-train", default=True)
@click.option("--train-batch-size", default=2)
@click.option("--num-train-epochs", default=5.0)
@click.option("--do-eval/--no-eval", default=True)
@click.option("--eval-batch-size", default=32)
@click.option("--train-on-dev-set", is_flag=True)
@click.option("--seed", default=35)
@trainer_args
@click.pass_obj
def run(common_args, **task_args):
    common_args.update(task_args)
    args = Namespace(**common_args)
    args.exp_name = get_exp_name(args.task_name)

    args.train_domains = args.train_domains.split(",")
    domain_label_indices, domain_features, all_entities = \
        load_domain_features(args, src_domain=args.dev_domain, trg_domain=args.test_domain, data_dir=args.data_dir)

    set_seed(args.seed)

    args.experiment.log_parameters({p.name: getattr(args, p.name) for p in run.params})

    args.model_config.entity_vocab_size = 2
    entity_emb = args.model_weights["entity_embeddings.entity_embeddings.weight"]
    mask_emb = entity_emb[args.entity_vocab[MASK_TOKEN]].unsqueeze(0)
    args.model_weights["entity_embeddings.entity_embeddings.weight"] = torch.cat([entity_emb[:1], mask_emb])

    train_source_dataloader, _, _, processor = load_and_cache_examples(args, "train", all_entities)
    train_target_dataloader, _, _, _ = load_and_cache_examples(args, "test", all_entities)
    results = {}

    if args.do_train:
        pretrained_luke = LukeForNamedEntityRecognition(args, len(processor.get_labels()))
        pretrained_luke.load_state_dict(args.model_weights, strict=False)

        dozen = Zero(args, pretrained_luke, domain_label_indices, domain_features)
        dozen.to(args.device)

        num_train_steps_per_epoch = len(train_source_dataloader) // args.gradient_accumulation_steps
        num_train_steps = int(num_train_steps_per_epoch * args.num_train_epochs)

        trainer = Trainer(args, model=dozen, all_entities=all_entities,
                          train_source_dataloader=train_source_dataloader,
                          train_target_dataloader=train_target_dataloader,
                          num_train_steps=num_train_steps)
        trainer.train()

        if args.local_rank in (0, -1):
            os.makedirs(os.path.join(args.output_dir, args.exp_name), exist_ok=True)
            logger.info("Saving the model checkpoint to %s", args.output_dir)
            dozen_path, luke_path, rgcn_path = get_saved_paths(args, tag="last")
            torch.save(pretrained_luke.state_dict(), luke_path)
            torch.save(dozen.state_dict(), dozen_path)

    if args.local_rank not in (0, -1):
        return {}

    torch.cuda.empty_cache()

    if args.do_eval:
        dozen_path, luke_path, rgcn_path = get_saved_paths(args, tag="latest")

        luke = LukeForNamedEntityRecognition(args, len(processor.get_labels()))
        luke.load_state_dict(torch.load(luke_path, map_location="cpu"))
        luke.to(args.device)

        zero = Zero(args, luke, domain_label_indices, domain_features)
        zero.load_state_dict(torch.load(dozen_path, map_location="cpu"))
        zero.to(args.device)

        train_output_file = os.path.join(args.output_dir, "train_predictions.txt")
        dev_output_file = os.path.join(args.output_dir, "dev_predictions.txt")
        test_output_file = os.path.join(args.output_dir, "test_predictions.txt")
        results.update({f"train_{k}": v for k, v in evaluate(args, zero, "train", train_output_file).items()})
        results.update({f"dev_{k}": v for k, v in evaluate(args, zero, "dev", dev_output_file).items()})
        results.update({f"test_{k}": v for k, v in evaluate(args, zero, "test", test_output_file).items()})

    logger.info("Results: %s", json.dumps(results, indent=2, sort_keys=True))
    args.experiment.log_metrics(results)
    with open(os.path.join(args.output_dir, "results.json"), "w") as f:
        json.dump(results, f)

    return results
