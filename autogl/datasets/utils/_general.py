import numpy as np
import random
import torch
import torch.utils.data
import typing as _typing
from sklearn.model_selection import StratifiedKFold, KFold
from dgl.dataloading.pytorch import GraphDataLoader
from autogl import backend as _backend
from autogl.data import Data, Dataset, DataLoader, InMemoryStaticGraphSet
from ...data.graph import GeneralStaticGraph, GeneralStaticGraphGenerator
from . import _pyg


def index_to_mask(index: torch.Tensor, size):
    mask = torch.zeros(size, dtype=torch.bool, device=index.device)
    mask[index] = True
    return mask


def split_edges(
        dataset: InMemoryStaticGraphSet,
        train_ratio: float, val_ratio: float
) -> InMemoryStaticGraphSet:
    test_ratio: float = 1 - train_ratio - val_ratio

    def _split_edges_for_graph(homogeneous_static_graph: GeneralStaticGraph) -> GeneralStaticGraph:
        if not isinstance(homogeneous_static_graph, GeneralStaticGraph):
            raise TypeError
        elif not homogeneous_static_graph.edges.is_homogeneous:
            raise ValueError("The provided graph MUST consist of homogeneous edges.")
        else:
            split_data = _pyg.train_test_split_edges(
                Data(
                    edge_index=homogeneous_static_graph.edges.connections.detach().clone(),
                    edge_attr=(
                        homogeneous_static_graph.edges.data['edge_attr'].detach().clone()
                        if 'edge_attr' in homogeneous_static_graph.edges.data else None
                    )
                ),
                val_ratio, test_ratio
            )
            original_edge_type = [et for et in homogeneous_static_graph.edges][0]

            split_static_graph = GeneralStaticGraphGenerator.create_heterogeneous_static_graph(
                dict([
                    (node_type, homogeneous_static_graph.nodes[node_type].data)
                    for node_type in homogeneous_static_graph.nodes
                ]),
                {
                    (original_edge_type.source_node_type, "train_pos_edge", original_edge_type.target_node_type): (
                        getattr(split_data, "train_pos_edge_index"),
                        {"edge_attr": getattr(split_data, "train_pos_edge_attr")}
                        if isinstance(getattr(split_data, "train_pos_edge_attr"), torch.Tensor)
                        else None
                    ),
                    (original_edge_type.source_node_type, "val_pos_edge", original_edge_type.target_node_type): (
                        getattr(split_data, "val_pos_edge_index"),
                        {"edge_attr": getattr(split_data, "val_pos_edge_attr")}
                        if isinstance(getattr(split_data, "val_pos_edge_attr"), torch.Tensor)
                        else None
                    ),
                    (original_edge_type.source_node_type, "val_neg_edge", original_edge_type.target_node_type):
                        getattr(split_data, "val_neg_edge_index"),
                    (original_edge_type.source_node_type, "test_pos_edge", original_edge_type.target_node_type): (
                        getattr(split_data, "test_pos_edge_index"),
                        {"edge_attr": getattr(split_data, "test_pos_edge_attr")}
                        if isinstance(getattr(split_data, "test_pos_edge_attr"), torch.Tensor)
                        else None
                    ),
                    (original_edge_type.source_node_type, "test_neg_edge", original_edge_type.target_node_type):
                        getattr(split_data, "test_neg_edge_index")
                },
                homogeneous_static_graph.data
            )
            return split_static_graph

    if not isinstance(dataset, InMemoryStaticGraphSet):
        raise TypeError
    for index in range(len(dataset)):
        dataset[index] = _split_edges_for_graph(dataset[index])
    return dataset


def random_splits_mask(
        dataset: InMemoryStaticGraphSet,
        train_ratio: float = 0.2, val_ratio: float = 0.4,
        seed: _typing.Optional[int] = None
) -> InMemoryStaticGraphSet:
    r"""If the data has masks for train/val/test, return the splits with specific ratio.

    Parameters
    ----------
    dataset : InMemoryStaticGraphSet
        graph set
    train_ratio : float
        the portion of data that used for training.

    val_ratio : float
        the portion of data that used for validation.

    seed : int
        random seed for splitting dataset.
    """
    if not train_ratio + val_ratio <= 1:
        raise ValueError("the sum of provided train_ratio and val_ratio is larger than 1")

    def __random_split_masks(
            num_nodes: int
    ) -> _typing.Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        _rng_state: torch.Tensor = torch.get_rng_state()
        if seed is not None and isinstance(seed, int):
            torch.manual_seed(seed)
        perm = torch.randperm(num_nodes)
        train_index = perm[:int(num_nodes * train_ratio)]
        val_index = perm[int(num_nodes * train_ratio): int(num_nodes * (train_ratio + val_ratio))]
        test_index = perm[int(num_nodes * (train_ratio + val_ratio)):]
        torch.set_rng_state(_rng_state)
        return (
            index_to_mask(train_index, num_nodes),
            index_to_mask(val_index, num_nodes),
            index_to_mask(test_index, num_nodes)
        )

    for index in range(len(dataset)):
        for node_type in dataset[index].nodes:
            data_keys = [data_key for data_key in dataset[index].nodes.data]
            if len(data_keys) > 0:
                _num_nodes: int = dataset[index].nodes[node_type].data[data_keys[0]].size(0)
                _masks: _typing.Tuple[torch.Tensor, torch.Tensor, torch.Tensor] = (
                    __random_split_masks(_num_nodes)
                )
                dataset[index].nodes[node_type].data["train_mask"] = _masks[0]
                dataset[index].nodes[node_type].data["val_mask"] = _masks[1]
                dataset[index].nodes[node_type].data["test_mask"] = _masks[2]
    return dataset


def random_splits_mask_class(
        dataset: InMemoryStaticGraphSet,
        num_train_per_class: int = 20,
        num_val_per_class: int = 30,
        total_num_val: _typing.Optional[int] = ...,
        total_num_test: _typing.Optional[int] = ...,
        seed: _typing.Optional[int] = ...
):
    r"""If the data has masks for train/val/test, return the splits with specific number of samples from every class for training as suggested in Pitfalls of graph neural network evaluation [#]_ for semi-supervised learning.

    References
    ----------
    .. [#] Shchur, O., Mumme, M., Bojchevski, A., & Günnemann, S. (2018).
        Pitfalls of graph neural network evaluation.
        arXiv preprint arXiv:1811.05868.

    Parameters
    ----------
    dataset: InMemoryStaticGraphSet
        instance of InMemoryStaticGraphSet
    num_train_per_class : int
        the number of samples from every class used for training.

    num_val_per_class : int
        the number of samples from every class used for validation.

    total_num_val : int
        the total number of nodes that used for validation as alternative.

    total_num_test : int
        the total number of nodes that used for testing as alternative. The rest of the data will be seleted as test set if num_test set to None.

    seed : int
        random seed for splitting dataset.
    """
    for graph_index in range(len(dataset)):
        for node_type in dataset[graph_index].nodes:
            if (
                    'y' in dataset[graph_index].nodes[node_type].data and
                    'label' in dataset[graph_index].nodes[node_type].data
            ):
                raise ValueError(
                    f"Both 'y' and 'label' data exist "
                    f"for node type [{node_type}] in "
                    f"graph with index [{graph_index}]."
                )
            elif (
                    'y' not in dataset[graph_index].nodes[node_type].data and
                    'label' not in dataset[graph_index].nodes[node_type].data
            ):
                continue
            elif 'y' in dataset[graph_index].nodes[node_type].data:
                label: torch.Tensor = dataset[graph_index].nodes[node_type].data['y']
            elif 'label' in dataset[graph_index].nodes[node_type].data:
                label: torch.Tensor = dataset[graph_index].nodes[node_type].data['label']
            else:
                raise RuntimeError
            num_nodes: int = label.size(0)
            num_classes: int = label.cpu().max().item() + 1

            _rng_state: torch.Tensor = torch.get_rng_state()
            if seed not in (Ellipsis, None) and isinstance(seed, int):
                torch.manual_seed(seed)
            train_mask = torch.zeros(num_nodes, dtype=torch.bool, device=label.device)
            val_mask = torch.zeros(num_nodes, dtype=torch.bool, device=label.device)
            test_mask = torch.zeros(num_nodes, dtype=torch.bool, device=label.device)
            for class_index in range(num_classes):
                idx = (label == class_index).nonzero().view(-1)
                assert num_train_per_class + num_val_per_class < idx.size(0), (
                    f"the total number of samples from every class "
                    f"used for training and validation is larger than "
                    f"the total samples in class [{class_index}] for node type [{node_type}] "
                    f"in graph with index [{graph_index}]"
                )
                randomized_index: torch.Tensor = torch.randperm(idx.size(0))
                train_idx = idx[randomized_index[:num_train_per_class]]
                val_idx = idx[
                    randomized_index[num_train_per_class: (num_train_per_class + num_val_per_class)]
                ]
                train_mask[train_idx] = True
                val_mask[val_idx] = True

            if isinstance(total_num_val, int) and total_num_val > 0:
                remaining = (~train_mask).nonzero().view(-1)
                remaining = remaining[torch.randperm(remaining.size(0))]
                val_mask[remaining[:total_num_val]] = True
                if isinstance(total_num_test, int) and total_num_test > 0:
                    test_mask[remaining[total_num_val: (total_num_val + total_num_test)]] = True
                else:
                    test_mask[remaining[total_num_val:]] = True
            else:
                remaining = (~(train_mask + val_mask)).nonzero().view(-1)
                test_mask[remaining] = True

            torch.set_rng_state(_rng_state)
            dataset[graph_index].nodes[node_type].data["train_mask"] = train_mask
            dataset[graph_index].nodes[node_type].data["val_mask"] = val_mask
            dataset[graph_index].nodes[node_type].data["test_mask"] = test_mask
    return dataset


def graph_cross_validation(
        dataset: InMemoryStaticGraphSet,
        n_splits: int = 10, shuffle: bool = True,
        random_seed: _typing.Optional[int] = ...,
        stratify: bool = False
) -> InMemoryStaticGraphSet:
    r"""Cross validation for graph classification data, returning one fold with specific idx in autogl.datasets or pyg.Dataloader(default)

    Parameters
    ----------
    dataset : str
        dataset with multiple graphs.

    n_splits : int
        the number of how many folds will be splitted.

    shuffle : bool
        shuffle or not for sklearn.model_selection.StratifiedKFold

    random_seed : int
        random_state for sklearn.model_selection.StratifiedKFold

    stratify: bool
    """
    if not isinstance(dataset, InMemoryStaticGraphSet):
        raise TypeError
    if not isinstance(n_splits, int):
        raise TypeError
    elif not n_splits > 0:
        raise ValueError
    if not isinstance(shuffle, bool):
        raise TypeError
    if not (random_seed in (Ellipsis, None) or isinstance(random_seed, int)):
        raise TypeError
    elif isinstance(random_seed, int) and random_seed >= 0:
        _random_seed: int = random_seed
    else:
        _random_seed: int = random.randrange(0, 65536)
    if not isinstance(stratify, bool):
        raise TypeError

    if stratify:
        kf = StratifiedKFold(
            n_splits=n_splits, shuffle=shuffle, random_state=_random_seed
        )
    else:
        kf = KFold(
            n_splits=n_splits, shuffle=shuffle, random_state=_random_seed
        )
    dataset_y = [g.data['y'].item() for g in dataset]
    idx_list = [
        (train_index.tolist(), test_index.tolist())
        for train_index, test_index
        in kf.split(np.zeros(len(dataset)), np.array(dataset_y))
    ]

    dataset.folds = idx_list
    dataset.train_index = idx_list[0][0]
    dataset.val_index = idx_list[0][1]
    return dataset


def graph_random_splits(
        dataset: InMemoryStaticGraphSet,
        train_ratio: float = 0.2,
        val_ratio: float = 0.4,
        seed: _typing.Optional[int] = ...
):
    r"""Splitting graph dataset with specific ratio for train/val/test.

    Parameters
    ----------
    dataset: ``InMemoryStaticGraphSet``

    train_ratio : float
        the portion of data that used for training.

    val_ratio : float
        the portion of data that used for validation.

    seed : int
        random seed for splitting dataset.
    """
    _rng_state = torch.get_rng_state()
    if isinstance(seed, int):
        torch.manual_seed(seed)
    perm = torch.randperm(len(dataset))
    train_index = perm[: int(len(dataset) * train_ratio)]
    val_index = (
        perm[int(len(dataset) * train_ratio): int(len(dataset) * (train_ratio + val_ratio))]
    )
    test_index = perm[int(len(dataset) * (train_ratio + val_ratio)):]
    dataset.train_index = train_index
    dataset.val_index = val_index
    dataset.test_index = test_index
    torch.set_rng_state(_rng_state)
    return dataset


def graph_get_split(
        dataset: Dataset, mask: str = "train",
        is_loader: bool = True, batch_size: int = 128,
        num_workers: int = 0
) -> _typing.Union[torch.utils.data.DataLoader, _typing.Iterable]:
    r"""Get train/test dataset/dataloader after cross validation.

    Parameters
    ----------
    dataset:
        dataset with multiple graphs.

    mask : str

    is_loader : bool
        return original dataset or data loader

    batch_size : int
        batch_size for generating Dataloader
    num_workers : int
        number of workers parameter for data loader
    """
    if not isinstance(dataset, Dataset):
        raise TypeError
    if not isinstance(mask, str):
        raise TypeError
    elif mask.lower() not in ("train", "val", "test"):
        raise ValueError
    if not isinstance(is_loader, bool):
        raise TypeError
    if not isinstance(batch_size, int):
        raise TypeError
    elif not batch_size > 0:
        raise ValueError
    if not isinstance(num_workers, int):
        raise TypeError
    elif not num_workers >= 0:
        raise ValueError

    if mask.lower() not in ("train", "val", "test"):
        raise ValueError
    elif mask.lower() == "train":
        optional_dataset_split = dataset.train_split
    elif mask.lower() == "val":
        optional_dataset_split = dataset.val_split
    elif mask.lower() == "test":
        optional_dataset_split = dataset.test_split
    else:
        raise ValueError(
            f"The provided mask parameter must be a str in ['train', 'val', 'test'], "
            f"illegal provided value is [{mask}]"
        )
    if (
            optional_dataset_split is None or
            not isinstance(optional_dataset_split, _typing.Iterable)
    ):
        raise ValueError(
            f"Provided dataset do NOT have {mask} split"
        )
    if is_loader:
        if not (_backend.DependentBackend.is_dgl() or _backend.DependentBackend.is_pyg()):
            raise RuntimeError("Unsupported backend")
        elif _backend.DependentBackend.is_dgl():
            return GraphDataLoader(
                optional_dataset_split,
                **{"batch_size": batch_size, "num_workers": num_workers}
            )
        elif _backend.DependentBackend.is_pyg():
            return DataLoader(
                optional_dataset_split,
                batch_size=batch_size,
                num_workers=num_workers
            )
    else:
        return optional_dataset_split
