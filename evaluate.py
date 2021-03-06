import os
import argparse
import re
from typing import Dict

from allennlp.common.params import Params
from allennlp.models.model import Model
from allennlp.predictors.seq2seq import Seq2SeqPredictor
from allennlp.data.dataset_readers.dataset_reader import DatasetReader
import torch
from rouge import Rouge

from summarus import *


def detokenize(text):
    punctuation = ",.!?:;%"
    closing_punctuation = ")]}"
    opening_punctuation = "([}"
    for ch in punctuation + closing_punctuation:
        text = text.replace(" " + ch, ch)
    for ch in opening_punctuation:
        text = text.replace(ch + " ", ch)
    res = [r'"\s[^"]+\s"', r"'\s[^']+\s'"]
    for r in res:
        for f in re.findall(r, text, re.U):
            text = text.replace(f, f[0] + f[2:-2] + f[-1])
    return text


def get_batches(reader: SummarizationReader, test_path: str, batch_size: int) -> Dict:
    batch = []
    for source, target in reader.parse_set(test_path):
        source = source.strip().lower()
        batch.append({"source": source, "target": target})
        if len(batch) == batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def evaluate(model_path, test_path, config_path, metric, is_multiple_ref, max_count, report_every, batch_size):
    params_path = config_path or os.path.join(model_path, "config.json")

    params = Params.from_file(params_path)
    is_subwords = "tokenizer" in params["reader"] and params["reader"]["tokenizer"]["type"] == "subword"
    reader = DatasetReader.from_params(params.pop("reader"))

    device = 0 if torch.cuda.is_available() else -1
    model = Model.load(params, model_path, cuda_device=device)
    model.training = False
    print(model)
    print("Trainable params count: ", sum(p.numel() for p in model.parameters() if p.requires_grad))

    hyps = []
    refs = []
    predictor = Seq2SeqPredictor(model, reader)
    for batch in get_batches(reader, test_path, batch_size):
        outputs = predictor.predict_batch_json(batch)
        targets = [b.get('target') for b in batch]
        for output, target in zip(outputs, targets):
            decoded_words = output["predicted_tokens"]
            if not is_multiple_ref:
                hyp = detokenize(" ".join(decoded_words)) if not is_subwords else "".join(decoded_words).replace("▁", " ")
                if len(hyp.strip()) <= 1:
                    hyp = "empty"
                    print("Empty hyp")
                if len(target.strip()) <= 1:
                    target = "empty"
                    print("Empty target")
                ref = [target]
            else:
                if isinstance(target, list):
                    reference_sents = target
                elif isinstance(target, str):
                    reference_sents = target.split(" s_s ")
                else:
                    assert False
                decoded_sents = (" ".join(decoded_words)).split("s_s")
                hyp = [w.replace("<", "&lt;").replace(">", "&gt;").strip() for w in decoded_sents]
                ref = [w.replace("<", "&lt;").replace(">", "&gt;").strip() for w in reference_sents]
                hyp = " ".join(hyp)
                ref = [" ".join(ref)]

            hyps.append(hyp)
            refs.append(ref)

            if len(hyps) % report_every == 0:
                print("Count: ", len(hyps))
                print("Ref: ", ref)
                print("Hyp: ", hyp)

                if metric in ("bleu", "all"):
                    from nltk.translate.bleu_score import corpus_bleu
                    print("BLEU: ", corpus_bleu(refs, hyps))

                if metric in ("rouge", "all"):
                    rouge = Rouge()
                    scores = rouge.get_scores(hyps, [r[0] for r in refs], avg=True)
                    print("ROUGE: ", scores)

            if max_count and len(hyps) >= max_count:
                break


def main(**kwargs):
    assert os.path.isdir(kwargs['model_path'])
    evaluate(**kwargs)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--model-path', required=True)
    parser.add_argument('--test-path', required=True)
    parser.add_argument('--config-path', default=None)
    parser.add_argument('--metric', choices=("rouge", "bleu", "all"), default="all")
    parser.add_argument('--is-multiple-ref', dest='is_multiple_ref', action='store_true')
    parser.add_argument('--max-count', type=int, default=None)
    parser.add_argument('--report-every', type=int, default=100)
    parser.add_argument('--batch-size', type=int, default=32)
    parser.set_defaults(is_multiple_ref=False)

    args = parser.parse_args()
    main(**vars(args))
