"""
Performance check of AutoGL Solver
"""

import os
os.environ["AUTOGL_BACKEND"] = "dgl"

import random
import numpy as np
from tqdm import tqdm

from autogl.solver import AutoGraphClassifier
from autogl.datasets import build_dataset_from_name
from autogl.solver.utils import set_seed
import logging

logging.basicConfig(level=logging.ERROR)

def fixed(**kwargs):
    return [{
        'parameterName': k,
        "type": "FIXED",
        "value": v
    } for k, v in kwargs.items()]

if __name__ == '__main__':

    import argparse
    parser = argparse.ArgumentParser('dgl solver')
    parser.add_argument('--device', type=str, default='cuda')
    parser.add_argument('--dataset', type=str, choices=[x.lower() for x in ['MUTAG', 'COLLAB', 'IMDBBINARY', 'IMDBMULTI', 'NCI1', 'PROTEINS', 'PTC', 'REDDITBINARY', 'REDDITMULTI5K']], default='mutag')
    parser.add_argument('--dataset_seed', type=int, default=2021)
    parser.add_argument('--batch_size', type=int, default=32)
    parser.add_argument('--repeat', type=int, default=50)
    parser.add_argument('--model', type=str, choices=['gin', 'gat', 'gcn', 'sage'], default='gin')
    parser.add_argument('--lr', type=float, default=0.0001)
    parser.add_argument('--epoch', type=int, default=100)

    args = parser.parse_args()

    # seed = 100
    dataset = build_dataset_from_name(args.dataset)
    
    # 1. split dataset [fix split]
    dataids = list(range(len(dataset)))
    random.seed(args.dataset_seed)
    random.shuffle(dataids)
    
    fold = int(len(dataset) * 0.1)
    dataset.train_index = dataids[:fold * 8]
    dataset.val_index = dataids[fold * 8: fold * 9]
    dataset.test_index = dataids[fold * 9: ]

    labels = np.array([x.data['label'].item() for x in dataset.test_split])

    accs = []
    for seed in tqdm(range(args.repeat)):
        set_seed(seed)
        solver = AutoGraphClassifier(
            feature_module=None,
            graph_models=[args.model],
            hpo_module='random',
            ensemble_module=None,
            device=args.device, max_evals=1,
            trainer_hp_space = fixed(**{
                    # hp from trainer
                    "max_epoch": 100,
                    "batch_size": 32,
                    "early_stopping_round": 101,
                    "lr": 0.0001,
                    "weight_decay": 0,
            }),
            model_hp_spaces=[
                fixed(**{
                    # hp from model
                    "num_layers": 5,
                    "hidden": [64],
                    "dropout": 0.5,
                    "act": "relu",
                    "eps": "False",
                    "mlp_layers": 2,
                    "neighbor_pooling_type": "sum",
                    "graph_pooling_type": "sum"
                }) if args.model == 'gin' else fixed(**{
                    "num_layers": 5,
                    "hidden": [64],
                    "dropout": 0.5
                }),
            ]
        )
        solver.fit(dataset, evaluation_method=['acc'])
        out = solver.predict(dataset, mask='test')
        acc = (out == labels).astype('float').mean()
        accs.append(acc)
    print('{:.4f} ~ {:.4f}'.format(np.mean(accs), np.std(accs)))
