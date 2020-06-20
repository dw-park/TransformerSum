import os
import logging
import random
import torch
import nlp
from functools import partial
from time import time
from collections import OrderedDict
from argparse import ArgumentParser
from torch import nn, optim
from rouge_score import rouge_scorer, scoring
from torch.utils.data import DataLoader
from torch.optim.lr_scheduler import LambdaLR, OneCycleLR
import pytorch_lightning as pl
from transformers import (
    BertForMaskedLM,
    BertModel,
    AutoTokenizer,
    EncoderDecoderModel,
)
from helpers import lr_lambda_func, pad

logger = logging.getLogger(__name__)


def trim_batch(
    input_ids, pad_token_id, attention_mask=None,
):
    """Remove columns that are populated exclusively by pad_token_id"""
    keep_column_mask = input_ids.ne(pad_token_id).any(dim=0)
    if attention_mask is None:
        return input_ids[:, keep_column_mask]
    else:
        return (input_ids[:, keep_column_mask], attention_mask[:, keep_column_mask])


class AbstractiveSummarizer(pl.LightningModule):
    def __init__(self, hparams):
        super(AbstractiveSummarizer, self).__init__()

        self.hparams = hparams

        self.model = EncoderDecoderModel.from_encoder_decoder_pretrained(
            self.hparams.model_name_or_path, self.hparams.model_name_or_path
        )

        self.tokenizer = AutoTokenizer.from_pretrained(
            self.hparams.model_name_or_path, use_fast=True
        )
        # bo = beginning of
        # eo = ending of
        # seq = sequence (not using 's' because 's' stands for sentence in other places)
        self.target_boseq_token = "[unused0]"
        self.target_eoseq_token = "[unused1]"
        self.target_boseq_token_id = self.tokenizer.convert_tokens_to_ids(
            self.target_boseq_token
        )
        self.target_eoseq_token_id = self.tokenizer.convert_tokens_to_ids(
            self.target_eoseq_token
        )

        # Add special tokens so that they are ignored when decoding.
        special_tokens_dict = {"additional_special_tokens": [self.target_boseq_token, self.target_eoseq_token]}
        self.tokenizer.add_special_tokens(special_tokens_dict)

        self.loss_func = nn.CrossEntropyLoss(ignore_index=self.tokenizer.pad_token_id)

    def forward(
        self, source=None, target=None, source_mask=None, target_mask=None, labels=None
    ):
        # `self.model.forward()` returns `decoder_outputs + encoder_outputs`
        outputs = self.model.forward(
            input_ids=source,
            attention_mask=source_mask,
            decoder_input_ids=target,
            decoder_attention_mask=target_mask,
            labels=labels,
        )

        cross_entropy_loss, prediction_scores = outputs[:2]
        return cross_entropy_loss, prediction_scores

    def prepare_data(self):
        def convert_to_features(example_batch):
            articles = example_batch[self.hparams.data_example_column]
            highlights = example_batch[self.hparams.data_summarized_column]
            articles_encoded = self.tokenizer.batch_encode_plus(
                articles, pad_to_max_length=True, truncation=True
            )
            # `max_length` is the max length minus 2 because we need to add the
            # beginning and ending tokens to the target
            highlights_input_ids = self.tokenizer.batch_encode_plus(
                highlights,
                truncation=True,
                max_length=(self.tokenizer.max_len - 2),
                return_attention_mask=False,
                return_token_type_ids=False,
            )["input_ids"]

            highlights_attention_masks = []
            # For each highlight input ids
            # 1. Insert beginning of sequence token and append end of sequence token.
            # 2. Create attention mask
            for input_ids in highlights_input_ids:
                input_ids.insert(0, self.target_boseq_token_id)
                input_ids.append(self.target_eoseq_token_id)

                attention_mask = [1] * len(input_ids)
                highlights_attention_masks.append(attention_mask)

            # Pad the highlight input ids and attention masks to `tokenizer.max_len`.
            # The articles have already been padded because they do not need the extra
            # `boseq` and `eoseq` tokens.
            highlights_input_ids = pad(
                highlights_input_ids,
                self.tokenizer.pad_token_id,
                width=self.tokenizer.max_len,
            )
            highlights_attention_masks = pad(
                highlights_attention_masks, 0, width=self.tokenizer.max_len
            )

            return {
                "source": articles_encoded["input_ids"],
                "target": highlights_input_ids,
                "source_mask": articles_encoded["attention_mask"],
                "target_mask": highlights_attention_masks,
            }

        self.dataset = nlp.load_dataset(
            self.hparams.dataset, self.hparams.dataset_version
        )

        self.dataset["train"] = self.dataset["train"].map(
            convert_to_features, batched=True, cache_file_name="train_tokenized"
        )
        self.dataset["validation"] = self.dataset["validation"].map(
            convert_to_features, batched=True, cache_file_name="validation_tokenized"
        )
        self.dataset["test"] = self.dataset["test"].map(
            convert_to_features, batched=True, cache_file_name="test_tokenized"
        )

        columns = ["source", "target", "source_mask", "target_mask"]
        self.dataset["train"].set_format(type="torch", columns=columns)
        self.dataset["validation"].set_format(type="torch", columns=columns)
        self.dataset["test"].set_format(type="torch", columns=columns)

    def train_dataloader(self):
        train_dataset = self.dataset["train"]

        train_dataloader = DataLoader(
            train_dataset,
            batch_size=self.hparams.batch_size,
            num_workers=self.hparams.dataloader_num_workers,
            pin_memory=True,
        )

        return train_dataloader

    def val_dataloader(self):
        val_dataset = self.dataset["validation"]

        val_dataloader = DataLoader(
            val_dataset,
            batch_size=(
                self.hparams.val_batch_size
                if self.hparams.val_batch_size
                else self.hparams.batch_size
            ),
            num_workers=self.hparams.dataloader_num_workers,
            pin_memory=True,
        )

        return val_dataloader

    def test_dataloader(self):
        self.rouge_metrics = ["rouge1", "rouge2", "rougeL"]
        self.rouge_scorer = rouge_scorer.RougeScorer(
            self.rouge_metrics, use_stemmer=True
        )

        self.hparams.test_batch_size = (
            self.hparams.test_batch_size
            if self.hparams.test_batch_size
            else self.hparams.batch_size
        )

        test_dataset = self.dataset["test"]

        test_dataloader = DataLoader(
            test_dataset,
            batch_size=self.hparams.test_batch_size,
            num_workers=self.hparams.dataloader_num_workers,
            pin_memory=True,
        )

        return test_dataloader

    def configure_optimizers(self):
        # create the train dataloader so the number of examples can be determined
        self.train_dataloader_object = self.train_dataloader()
        # check that max_steps is not None and is greater than 0
        if self.hparams.max_steps and self.hparams.max_steps > 0:
            # pytorch_lightning steps the scheduler every batch but only updates
            # the global_step every gradient accumulation cycle. Therefore, the
            # scheduler needs to have `accumulate_grad_batches` * `max_steps` in
            # order to reach `max_steps`.
            # See: https://github.com/PyTorchLightning/pytorch-lightning/blob/f293c9b5f4b4f9fabb2eec0c369f08a66c57ef14/pytorch_lightning/trainer/training_loop.py#L624
            t_total = self.hparams.max_steps * self.hparams.accumulate_grad_batches
        else:
            t_total = int(
                len(self.train_dataloader_object)
                * self.hparams.max_epochs
                // self.hparams.accumulate_grad_batches
            )
            if self.hparams.overfit_pct > 0.0:
                t_total = int(t_total * self.hparams.overfit_pct)

        no_decay = ["bias", "LayerNorm.weight"]
        optimizer_grouped_parameters = [
            {
                "params": [
                    p
                    for n, p in self.named_parameters()
                    if not any(nd in n for nd in no_decay)
                ],
                "weight_decay": self.hparams.weight_decay,
            },
            {
                "params": [
                    p
                    for n, p in self.named_parameters()
                    if any(nd in n for nd in no_decay)
                ],
                "weight_decay": 0.0,
            },
        ]

        optimizer = optim.AdamW(
            optimizer_grouped_parameters,
            lr=self.hparams.learning_rate,
            eps=self.hparams.adam_epsilon,
        )

        if self.hparams.use_scheduler:
            if self.hparams.use_scheduler == "linear":
                # We have to import the function and create a partial because functions cannot be
                # serialized by python pickle. Therefore, if the normal `get_linear_schedule_with_warmup`
                # function provided by `transformers` was used, the program would fail to save
                # `self.hparams` because the optimizer would contain a locale function that cannot be
                # pickled.
                lr_lambda = partial(
                    lr_lambda_func,
                    num_warmup_steps=self.hparams.warmup_steps
                    * self.hparams.accumulate_grad_batches,
                    num_training_steps=t_total,
                )
                # multiply by `hparams.accumulate_grad_batches` above because pytorch_lightning
                # steps are for each batch, except for the `trainer.global_step`, which tracks
                # the actual number of steps

                scheduler = LambdaLR(optimizer, lr_lambda, -1)

            elif self.hparams.use_scheduler == "onecycle":
                scheduler = torch.optim.lr_scheduler.OneCycleLR(
                    optimizer, max_lr=self.hparams.learning_rate, total_steps=t_total
                )
            else:
                logger.error(
                    "The value "
                    + str(self.hparams.use_scheduler)
                    + " for `--use_scheduler` is invalid."
                )
            # the below interval is called "step" but the scheduler is moved forward
            # every batch.
            scheduler_dict = {"scheduler": scheduler, "interval": "step"}

            return ([optimizer], [scheduler_dict])
        else:
            return optimizer

    def _step(self, batch):
        source, target, source_mask, target_mask = (
            batch["source"],
            batch["target"],
            batch["source_mask"],
            batch["target_mask"],
        )
        labels = target.clone()
        labels[labels == 0] = -100  # -100 index = padding token
        outputs = self.forward(source, target, source_mask, target_mask, labels=labels)
        loss = outputs[0]
        return loss

    def training_step(self, batch, batch_idx):
        cross_entropy_loss = self._step(batch)

        tqdm_dict = {"train_loss": cross_entropy_loss}
        output = OrderedDict(
            {"loss": cross_entropy_loss, "progress_bar": tqdm_dict, "log": tqdm_dict,}
        )
        return output

    def validation_step(self, batch, batch_idx):
        cross_entropy_loss = self._step(batch)

        tqdm_dict = {"val_loss": cross_entropy_loss}
        output = OrderedDict(
            {
                "val_loss": cross_entropy_loss,
                "progress_bar": tqdm_dict,
                "log": tqdm_dict,
            }
        )
        return output

    def validation_epoch_end(self, outputs):
        avg_loss = torch.stack([x["val_loss"] for x in outputs]).mean()

        tqdm_dict = {"val_loss": avg_loss}
        output = {
            "val_loss": avg_loss,
            "progress_bar": tqdm_dict,
            "log": tqdm_dict,
        }
        return output

    def test_step(self, batch, batch_idx):
        source_ids, target_ids, source_mask, _ = batch.values()

        source_ids, source_mask = trim_batch(
            source_ids, self.tokenizer.pad_token_id, attention_mask=source_mask
        )
        target_ids = trim_batch(target_ids, self.tokenizer.pad_token_id)

        # Generate
        # Set `pad_token_id` to `self.target_eoseq_token_id`, which is the same as
        # `eos_token_id` in order to skip a warning. The `generate` function will
        # do this if we don't, but when we do it the warning does not occur.
        t0 = time()
        generated_ids = self.model.generate(
            input_ids=source_ids,
            attention_mask=source_mask,
            num_beams=5,
            decoder_start_token_id=self.target_boseq_token_id,
            bos_token_id=self.target_boseq_token_id,
            eos_token_id=self.target_eoseq_token_id,
            pad_token_id=self.target_eoseq_token_id,
            max_length=self.tokenizer.max_len,
            no_repeat_ngram_size=3,
            use_cache=True,
        )
        generation_time = time() - t0
        logger.debug("Generation Time: {}".format(generation_time))

        generated_ids = generated_ids.tolist()
        target_ids = target_ids.tolist()

        predictions = self.ids_to_clean_text(generated_ids)
        targets = self.ids_to_clean_text(target_ids)

        cross_entropy_loss, prediction_scores = self.forward(**batch)

        rouge_outputs = []
        for target, prediction in zip(targets, predictions):
            rouge_outputs.append(self.rouge_scorer.score(target, prediction))

        # Save about `self.hparams.save_percentage` of the predictions and targets
        # if `self.hparams.save_percentage` is set.
        if (
            self.hparams.save_percentage
            and random.random() < self.hparams.save_percentage
        ):
            index_to_select = random.randrange(0, self.hparams.test_batch_size, 1)
            output_prediction = predictions[index_to_select]
            output_target = targets[index_to_select]
        else:
            output_prediction = None
            output_target = None

        output = OrderedDict(
            {
                "rouge_scores": rouge_outputs,
                "generation_time": generation_time,
                "prediction": output_prediction,
                "target": output_target,
            }
        )
        return output

    def test_epoch_end(self, outputs):
        avg_generation_time = torch.stack(
            [x["generation_time"] for x in outputs]
        ).mean()

        rouge_scores_log = {}
        aggregator = scoring.BootstrapAggregator()
        rouge_scores_list = [
            rouge_score_set
            for batch_list in outputs
            for rouge_score_set in batch_list["rouge_scores"]
        ]
        for score in rouge_scores_list:
            aggregator.add_scores(score)
        # The aggregator returns a dictionary with keys coresponding to the rouge metric
        # and values that are `AggregateScore` objects. Each `AggregateScore` object is a
        # named tuple with a low, mid, and high value. Each value is a `Score` object, which
        # is also a named tuple, that contains the precision, recall, and fmeasure values.
        # For more info see the source code: https://github.com/google-research/google-research/blob/master/rouge/scoring.py
        rouge_result = aggregator.aggregate()

        for metric, value in rouge_result.items():
            rouge_scores_log[metric + "-precision"] = value.mid.precision
            rouge_scores_log[metric + "-recall"] = value.mid.recall
            rouge_scores_log[metric + "-fmeasure"] = value.mid.fmeasure

        # Write the saved predictions and targets to file
        if self.hparams.save_percentage:
            predictions = [
                x["prediction"] for x in outputs if x["prediction"] is not None
            ]
            targets = [x["target"] for x in outputs if x["target"] is not None]
            output_test_predictions_file = os.path.join(
                self.hparams.default_root_dir, "test_predictions.txt"
            )
            output_test_targets_file = os.path.join(
                self.hparams.default_root_dir, "test_targets.txt"
            )
            with open(output_test_predictions_file, "w+") as p_writer, open(
                output_test_targets_file, "w+"
            ) as t_writer:
                for prediction, target in zip(predictions, targets):
                    p_writer.writelines(s + "\n" for s in prediction)
                    t_writer.writelines(s + "\n" for s in target)
                p_writer.close()
                t_writer.close()

        # Generate logs
        other_stats = {"generation_time": avg_generation_time}
        tqdm_dict = {
            "rouge1-fmeasure": rouge_scores_log["rouge1-fmeasure"],
            "rouge2-fmeasure": rouge_scores_log["rouge2-fmeasure"],
            "rougeL-fmeasure": rouge_scores_log["rougeL-fmeasure"],
            "generation_time": avg_generation_time,
        }
        log = {**rouge_scores_log, **other_stats}
        result = {"progress_bar": tqdm_dict, "log": rouge_scores_log}
        return result

    def predict(self, input_sequence):
        # If a single string is passed, wrap it in a list so `batch_encode_plus()`
        # processes it correctly
        if type(input_sequence) is str:
            input_sequence = [input_sequence]

        input_sequence_encoded = self.tokenizer.batch_encode_plus(
            input_sequence,
            pad_to_max_length=False,
            truncation=True,
            return_attention_mask=False,
            return_token_type_ids=False,
        )["input_ids"]
        input_sequence_encoded = torch.tensor(input_sequence_encoded)

        t0 = time()
        generated_ids = self.model.generate(
            input_ids=input_sequence_encoded,
            num_beams=3,
            decoder_start_token_id=self.target_boseq_token_id,
            bos_token_id=self.target_boseq_token_id,
            eos_token_id=self.target_eoseq_token_id,
            pad_token_id=self.target_eoseq_token_id,
            max_length=self.tokenizer.max_len,
            no_repeat_ngram_size=3,
            use_cache=True,
        )
        generation_time = time() - t0
        logger.debug("Generation Time: {}".format(generation_time))

        generated_ids = generated_ids.tolist()
        prediction = self.ids_to_clean_text(generated_ids)

        return prediction

    def ids_to_clean_text(self, generated_ids):
        gen_text = self.tokenizer.batch_decode(
            generated_ids, skip_special_tokens=True, clean_up_tokenization_spaces=True
        )

        return list(map(str.strip, gen_text))

    @pl.utilities.rank_zero_only
    def on_save_checkpoint(self, checkpoint):
        if self.hparams.save_hg_transformer:
            save_path = os.path.join(self.hparams.weights_save_path, "best_tfmr")

            if not os.path.exists(save_path):
                os.makedirs(save_path)

            self.model.save_pretrained(save_path)
            self.tokenizer.save_pretrained(save_path)

    @staticmethod
    def add_model_specific_args(parent_parser):
        parser = ArgumentParser(parents=[parent_parser])
        parser.add_argument(
            "--model_name_or_path",
            type=str,
            default="bert-base-uncased",
            help="Path to pre-trained model or shortcut name. A list of shortcut names can be found at https://huggingface.co/transformers/pretrained_models.html. Community-uploaded models are located at https://huggingface.co/models.",
        )
        parser.add_argument(
            "--batch_size",
            default=4,
            type=int,
            help="Batch size per GPU/CPU for training/evaluation/testing.",
        )
        parser.add_argument(
            "--val_batch_size",
            default=None,
            type=int,
            help="Batch size per GPU/CPU for evaluation. This option overwrites `--batch_size` for evaluation only.",
        )
        parser.add_argument(
            "--test_batch_size",
            default=None,
            type=int,
            help="Batch size per GPU/CPU for testing. This option overwrites `--batch_size` for testing only.",
        )
        parser.add_argument(
            "--dataloader_num_workers",
            default=3,
            type=int,
            help="The number of workers to use when loading data. A general place to start is to set num_workers equal to the number of CPUs on your machine. More details here: https://pytorch-lightning.readthedocs.io/en/latest/performance.html#num-workers",
        )
        parser.add_argument(
            "--adam_epsilon",
            default=1e-8,
            type=float,
            help="Epsilon for Adam optimizer.",
        )
        parser.add_argument(
            "--warmup_steps",
            default=0,
            type=int,
            help="Linear warmup over warmup_steps. Only active if `--use_scheduler` is set.",
        )
        parser.add_argument(
            "--use_scheduler",
            default=False,
            help="""Two options:
            1. `linear`: Use a linear schedule that inceases linearly over `--warmup_steps` to `--learning_rate` then decreases linearly for the rest of the training process.
            2. `onecycle`: Use the one cycle policy with a maximum learning rate of `--learning_rate`.
            (default: False, don't use any scheduler)""",
        )
        parser.add_argument("--weight_decay", default=1e-2, type=float)
        parser.add_argument(
            "--dataset",
            type=str,
            default="cnn_dailymail",
            help="The dataset name from the `nlp` library to use for training/evaluation/testing. Default is `cnn_dailymail`.",
        )
        parser.add_argument(
            "--dataset_version",
            type=str,
            default="3.0.0",
            help="The version of the dataset specified by `--dataset`.",
        )
        parser.add_argument(
            "--data_example_column",
            type=str,
            default="article",
            help="The column of the `nlp` dataset that contains the text to be summarized. Default value is for the `cnn_dailymail` dataset.",
        )
        parser.add_argument(
            "--data_summarized_column",
            type=str,
            default="highlights",
            help="The column of the `nlp` dataset that contains the summarized text. Default value is for the `cnn_dailymail` dataset.",
        )
        parser.add_argument(
            "--save_percentage",
            type=float,
            default=0.01,
            help="""Percentage (divided by batch_size) between 0 and 1 of the predicted and target 
            summaries from the test set to save to disk during testing. This depends on batch 
            size: one item from each batch is saved `--save_percentage` percent of the time. 
            Thus, you can expect `len(dataset)*save_percentage/batch_size` summaries to be saved.""",
        )
        parser.add_argument(
            "--save_hg_transformer",
            action="store_true",
            help="Save the `huggingface/transformers` model whenever a checkpoint is saved.",
        )

        return parser


# test = AbstractiveSummarizer(["test"])