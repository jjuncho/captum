#!/usr/bin/env fbpython
from typing import cast

import torch

from captum.attr._core.dataloader_attr import DataloaderAttribution, InputRole
from captum.attr._core.feature_ablation import FeatureAblation
from parameterized import parameterized
from tests.helpers.basic import (
    assertAttributionComparision,
    assertTensorAlmostEqual,
    BaseTest,
)
from torch import Tensor


def sum_forward(*inps):
    inps = [torch.flatten(inp, start_dim=1) for inp in inps]
    return torch.cat(inps, dim=1).sum(1)


class Linear(torch.nn.Module):
    def __init__(self, n):
        super().__init__()
        self.linear = torch.nn.Linear(n, 1)

    def forward(self, *inps):
        inps = [torch.flatten(inp, start_dim=1) for inp in inps]
        return self.linear(torch.cat(inps, dim=1))


mock_dataset = torch.utils.data.TensorDataset(
    # iD feature
    torch.tensor(
        [
            [0.0, 0.1],
            [0.3, 0.4],
            [0.6, 0.7],
            [0.9, 1.0],
            [1.2, 1.3],
        ]
    ),
    # 2D feature
    torch.tensor(
        [
            [[0.1, 0.2], [0.3, 0.2]],
            [[0.4, 0.5], [0.3, 0.2]],
            [[0.8, 0.1], [0.2, 0.5]],
            [[1.1, 0.7], [0.1, 0.7]],
            [[0.6, 1.4], [1.2, 0.4]],
        ]
    ),
    # scalar feature or label
    torch.tensor(
        [
            [0],
            [1],
            [0],
            [0],
            [1],
        ]
    ),
)


class Test(BaseTest):
    @parameterized.expand(
        [
            (sum_forward,),
            (Linear(7),),
        ]
    )
    def test_dl_attr(self, forward) -> None:
        fa = FeatureAblation(forward)
        dl_fa = DataloaderAttribution(fa)

        dataloader = torch.utils.data.DataLoader(mock_dataset, batch_size=2)

        dl_attributions = dl_fa.attribute(dataloader)

        # default reduce of DataloaderAttribution works the same as concat all batches
        attr_list = []
        for batch in dataloader:
            batch_attr = fa.attribute(tuple(batch))
            attr_list.append(batch_attr)

        expected_attr = tuple(
            torch.cat(feature_attrs, dim=0) for feature_attrs in zip(*attr_list)
        )

        assertAttributionComparision(self, dl_attributions, expected_attr)

    @parameterized.expand(
        [
            (sum_forward,),
            (Linear(7),),
        ]
    )
    def test_dl_attr_with_mask(self, forward) -> None:
        # FeatureAblation does not support grouping across tensors for now
        # add such test cases after support grouping across tensors in FeatureAblation
        masks = (
            torch.tensor([[0, 0]]),
            torch.tensor([[[1, 2], [3, 2]]]),
            torch.tensor([[4]]),
        )

        fa = FeatureAblation(forward)
        dl_fa = DataloaderAttribution(fa)

        dataloader = torch.utils.data.DataLoader(mock_dataset, batch_size=2)

        dl_attributions = dl_fa.attribute(dataloader, feature_mask=masks)

        # default reduce of DataloaderAttribution works the same as concat all batches
        attr_list = []
        for batch in dataloader:
            batch_attr = fa.attribute(tuple(batch), feature_mask=masks)
            attr_list.append(batch_attr)

        expected_attr = tuple(
            torch.cat(feature_attrs, dim=0) for feature_attrs in zip(*attr_list)
        )

        assertAttributionComparision(self, dl_attributions, expected_attr)

    @parameterized.expand(
        [
            (sum_forward,),
            (Linear(7),),
        ]
    )
    def test_dl_attr_with_baseline(self, forward) -> None:
        baselines = (
            torch.tensor([[0, -1]]),
            1,
            0.1,
        )

        fa = FeatureAblation(forward)
        dl_fa = DataloaderAttribution(fa)

        dataloader = torch.utils.data.DataLoader(mock_dataset, batch_size=2)

        dl_attributions = dl_fa.attribute(dataloader, baselines=baselines)

        # default reduce of DataloaderAttribution works the same as concat all batches
        attr_list = []
        for batch in dataloader:
            batch_attr = fa.attribute(tuple(batch), baselines=baselines)
            attr_list.append(batch_attr)

        expected_attr = tuple(
            torch.cat(feature_attrs, dim=0) for feature_attrs in zip(*attr_list)
        )

        assertAttributionComparision(self, dl_attributions, expected_attr)

    def test_dl_attr_with_reduce_and_to_metric(self) -> None:
        forward = sum_forward
        func_call_counts = {
            "reduce": 0,
            "to_metric": 0,
        }

        def reduce(accum, cur_output, cur_inputs):
            func_call_counts["reduce"] += 1

            accum = {"sum": 0, "count": 0} if accum is None else accum

            accum["sum"] += cur_output.sum()
            accum["count"] += len(cur_output)

            return accum

        def to_metric(accum):
            func_call_counts["to_metric"] += 1

            self.assertEqual(isinstance(accum, dict), True)
            return torch.tensor(
                [
                    accum["sum"] / accum["count"],
                    accum["sum"],
                ]
            )

        fa = FeatureAblation(forward)
        dl_fa = DataloaderAttribution(fa)

        batch_size = 2
        dataloader = torch.utils.data.DataLoader(mock_dataset, batch_size=batch_size)

        dl_attribution = dl_fa.attribute(
            dataloader,
            reduce=reduce,
            to_metric=to_metric,
            return_input_shape=False,
        )

        n_iters = len(dataloader)

        n_features = 7
        # after support other attr methods, this can be diff from n_features
        n_perturbations = 7
        n_passes = n_perturbations + 1  # +1 for base forward without perturbation
        n_outputs = 2  # [mean, sum]

        self.assertEqual(func_call_counts["reduce"], n_iters * n_passes)
        self.assertEqual(func_call_counts["to_metric"], n_passes)

        expected_attr_shape = (n_outputs, n_features)

        self.assertEqual(type(dl_attribution), Tensor)
        dl_attribution = cast(Tensor, dl_attribution)
        self.assertEqual(dl_attribution.shape, expected_attr_shape)

    @parameterized.expand(
        [
            ([0, 0, 0],),
            ([0, 1, 0],),
            ([0, 1, 1],),
            ([0, 1, 2],),
            ([0, 2, 2],),
        ]
    )
    def test_dl_attr_with_input_roles(self, input_roles) -> None:
        n_inputs = len(input_roles)
        n_forward_inputs = sum(1 for r in input_roles if r != InputRole.no_forward)
        n_attr_inputs = sum(1 for r in input_roles if r == InputRole.need_attr)

        def reduce(accum, cur_output, cur_inputs):
            # all inputs from dataloader should be given to reduce
            self.assertEqual(len(cur_inputs), n_inputs)

            return cur_output if accum is None else torch.cat([accum, cur_output])

        def forward(*forward_inputs):
            # inputs of InputRole.no_forward should not be passed to forward
            self.assertEqual(len(forward_inputs), n_forward_inputs)
            return sum_forward(*forward_inputs)

        fa = FeatureAblation(forward)
        dl_fa = DataloaderAttribution(fa)

        batch_size = 2
        dataloader = torch.utils.data.DataLoader(mock_dataset, batch_size=batch_size)

        dl_attributions = dl_fa.attribute(
            dataloader,
            input_roles=input_roles,
            reduce=reduce,
        )

        # only inputs needs
        self.assertEqual(len(dl_attributions), n_attr_inputs)

        # default reduce of DataloaderAttribution works the same as concat all batches
        attr_list = []
        for batch in dataloader:
            attr_inputs = tuple(
                _ for _, role in zip(batch, input_roles) if role == InputRole.need_attr
            )
            additional_forward_args = tuple(
                _
                for _, role in zip(batch, input_roles)
                if role == InputRole.need_forward
            )

            batch_attr = fa.attribute(
                attr_inputs, additional_forward_args=additional_forward_args
            )
            attr_list.append(batch_attr)

        expected_attr = tuple(
            torch.cat(feature_attrs, dim=0) for feature_attrs in zip(*attr_list)
        )

        assertAttributionComparision(self, dl_attributions, expected_attr)

    def test_dl_attr_not_return_input_shape(self) -> None:
        forward = sum_forward
        fa = FeatureAblation(forward)
        dl_fa = DataloaderAttribution(fa)

        dataloader = torch.utils.data.DataLoader(mock_dataset, batch_size=2)

        dl_attribution = dl_fa.attribute(dataloader, return_input_shape=False)

        expected_attr_shape = (len(mock_dataset), 7)

        self.assertEqual(type(dl_attribution), Tensor)
        dl_attribution = cast(Tensor, dl_attribution)
        self.assertEqual(dl_attribution.shape, expected_attr_shape)

        # default reduce of DataloaderAttribution works the same as concat all batches
        attr_list = []
        for batch in dataloader:
            batch_attr = fa.attribute(tuple(batch))
            attr_list.append(batch_attr)

        expected_attr = torch.cat(
            [
                # flatten feature dim
                torch.cat(feature_attrs, dim=0).flatten(start_dim=1)
                for feature_attrs in zip(*attr_list)
            ],
            dim=1,
        )

        assertTensorAlmostEqual(self, dl_attribution, expected_attr)

    def test_dl_attr_with_mask_not_return_input_shape(self) -> None:
        forward = sum_forward
        masks = (
            torch.tensor([[0, 0]]),
            torch.tensor([[[1, 2], [3, 2]]]),
            torch.tensor([[4]]),
        )

        fa = FeatureAblation(forward)
        dl_fa = DataloaderAttribution(fa)

        dataloader = torch.utils.data.DataLoader(mock_dataset, batch_size=2)

        dl_attribution = dl_fa.attribute(
            dataloader, feature_mask=masks, return_input_shape=False
        )

        expected_attr_shape = (len(mock_dataset), 5)

        self.assertEqual(type(dl_attribution), Tensor)
        dl_attribution = cast(Tensor, dl_attribution)
        self.assertEqual(dl_attribution.shape, expected_attr_shape)
