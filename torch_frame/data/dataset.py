import copy
import functools
from abc import ABC
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple, Union

import pandas as pd
import torch
from torch import Tensor

import torch_frame
from torch_frame.data import TensorFrame
from torch_frame.data.mapper import (
    CategoricalTensorMapper,
    NumericalTensorMapper,
)
from torch_frame.data.stats import StatType, compute_col_stats
from torch_frame.typing import ColumnSelectType, DataFrame, IndexSelectType


def requires_pre_materialization(func):
    @functools.wraps(func)
    def _requires_pre_materialization(self, *args, **kwargs):
        if self.is_materialized:
            raise RuntimeError(
                f"'{self}' cannot be modified via '{func.__name__}' post "
                f"materialization")
        return func(self, *args, **kwargs)

    return _requires_pre_materialization


def requires_post_materialization(func):
    @functools.wraps(func)
    def _requires_post_materialization(self, *args, **kwargs):
        if not self.is_materialized:
            raise RuntimeError(
                f"'{func.__name__}' requires a materialized dataset. Please "
                f"call `dataset.materialize(...)` first.")
        return func(self, *args, **kwargs)

    return _requires_post_materialization


class Dataset(ABC):
    r"""Base class for creating tabular datasets.

    Args:
        df (DataFrame): The tabular data frame.
        col_to_stype (Dict[str, torch_frame.stype]): A dictionary that maps
            each column in the data frame to a semantic type.
        target_col (str, optional): The column used as target.
    """
    def __init__(
        self,
        df: DataFrame,
        col_to_stype: Dict[str, torch_frame.stype],
        target_col: Optional[str] = None,
    ):
        self.df = df
        self.col_to_stype = col_to_stype
        self.target_col = target_col

        cols = self.feat_cols + ([] if target_col is None else [target_col])
        missing_cols = set(cols) - set(df.columns)
        if len(missing_cols) > 0:
            raise ValueError(f"The column(s) '{missing_cols}' are specified "
                             f"but missing in the data frame")

        self._is_materialized: bool = False
        self._col_stats: Dict[str, Dict[StatType, Any]] = {}
        self._tensor_frame: Optional[TensorFrame] = None

    @staticmethod
    def download_url(
        url: str,
        root: str,
        filename: Optional[str] = None,
        *,
        log: bool = True,
    ) -> str:
        r"""Downloads the content of :obj:`url` to the specified folder
        :obj:`root`.

        Args:
            url (str): The URL.
            root (str): The root folder.
            filename (str, optional): If set, will rename the downloaded file.
                (default: :obj:`None`)
            log (bool, optional): If :obj:`False`, will not print anything to
                the console. (default: :obj:`True`)
        """
        return torch_frame.data.download_url(url, root, filename, log=log)

    def __repr__(self) -> str:
        return f'{self.__class__.__name__}()'

    def __len__(self) -> int:
        return len(self.df)

    def __getitem__(self, index: IndexSelectType) -> 'Dataset':
        is_col_select = isinstance(index, str)
        is_col_select |= (isinstance(index, (list, tuple)) and len(index) > 0
                          and isinstance(index[0], str))

        if is_col_select:
            return self.col_select(index)

        return self.index_select(index)

    @property
    def feat_cols(self) -> List[str]:
        r"""The input feature columns of the dataset."""
        cols = list(self.col_to_stype.keys())
        if self.target_col is not None:
            cols.remove(self.target_col)
        return cols

    # Materialization #########################################################

    def materialize(self, device: Optional[torch.device] = None) -> 'Dataset':
        r"""Materializes the dataset into a tensor representation. From this
        point onwards, the dataset should be treated as read-only."""
        if self.is_materialized:
            return self

        # 1. Fill column statistics:
        for col, stype in self.col_to_stype.items():
            self._col_stats[col] = compute_col_stats(self.df[col], stype)

        # 2. Create the `TensorFrame`:
        self._tensor_frame = self._to_tensor_frame(device)

        # 3. Mark the dataset as materialized:
        self._is_materialized = True

        return self

    @property
    def is_materialized(self) -> bool:
        r"""Whether the dataset is already materialized."""
        return self._is_materialized

    @property
    @requires_post_materialization
    def tensor_frame(self) -> TensorFrame:
        r"""Returns the :class:`TensorFrame` of the dataset."""
        return self._tensor_frame

    @property
    @requires_post_materialization
    def col_stats(self) -> Dict[str, Dict[StatType, Any]]:
        r"""Returns column-wise dataset statistics."""
        return self._col_stats

    def _to_tensor_frame(
        self,
        device: Optional[torch.device] = None,
    ) -> TensorFrame:

        xs_dict: Dict[torch_frame.stype, List[Tensor]] = defaultdict(list)
        col_names_dict: Dict[torch_frame.stype, List[str]] = defaultdict(list)
        y: Optional[Tensor] = None

        for col, stype in self.col_to_stype.items():

            if stype == torch_frame.numerical:
                mapper = NumericalTensorMapper()

            elif stype == torch_frame.categorical:
                index, value = self._col_stats[col][StatType.COUNT]

                if col == self.target_col and len(index) == 2:
                    # Sort categories lexicographically such that we do not
                    # accidentially swap labels in binary classification tasks.
                    ser = pd.Series(index=index, data=value).sort_index()
                    index, value = ser.index.tolist(), ser.values.tolist()
                    self._col_stats[col][StatType.COUNT] = (index, value)

                mapper = CategoricalTensorMapper(index)

            else:
                raise NotImplementedError(f"Unable to process the semantic "
                                          f"type '{stype.value}'")

            out = mapper.forward(self.df[col], device=device)

            if col == self.target_col:
                y = out
            else:
                xs_dict[stype].append(out)
                col_names_dict[stype].append(col)

        x_dict = {
            stype: torch.stack(xs, dim=1)
            for stype, xs in xs_dict.items()
        }

        return TensorFrame(x_dict, col_names_dict, y)

    # Indexing ################################################################

    @requires_post_materialization
    def index_select(self, index: IndexSelectType) -> 'Dataset':
        r"""Returns a subset of the dataset from specified indices
        :obj:`index`."""
        if isinstance(index, int):
            index = [index]

        elif isinstance(index, slice):
            start, stop, step = index.start, index.stop, index.step
            # Allow floating-point slicing, e.g., dataset[:0.9]
            if isinstance(start, float):
                start = round(start * len(self))
            if isinstance(stop, float):
                stop = round(stop * len(self))
            index = slice(start, stop, step)

        dataset = copy.copy(self)

        iloc = index.cpu().numpy() if isinstance(index, Tensor) else index
        dataset.df = self.df.iloc[iloc]

        dataset._tensor_frame = self._tensor_frame[index]

        return dataset

    def shuffle(
        self, return_perm: bool = False
    ) -> Union['Dataset', Tuple['Dataset', Tensor]]:
        r"""Randomly shuffles the rows in the dataset."""
        perm = torch.randperm(len(self))
        dataset = self.index_select(perm)
        return (dataset, perm) if return_perm is True else dataset

    @requires_pre_materialization
    def col_select(self, cols: ColumnSelectType) -> 'Dataset':
        r"""Returns a subset of the dataset from specified columns
        :obj:`cols`."""
        cols = [cols] if isinstance(cols, str) else cols

        if self.target_col is not None and self.target_col not in cols:
            cols.append(self.target_col)

        dataset = copy.copy(self)

        dataset.df = self.df[cols]
        dataset.col_to_stype = {col: self.col_to_stype[col] for col in cols}

        return dataset