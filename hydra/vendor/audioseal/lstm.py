# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#
# Vendor from https://github.com/facebookresearch/audiocraft
# Vendored into HYDRA NEXUS from https://github.com/facebookresearch/audioseal @ 1496f7662d6aa625602bb0dcd1b4997b63b47609,
# file src/audioseal/libs/audiocraft/modules/lstm.py (MIT, see hydra/vendor/audioseal/LICENSE). Modification by HYDRA
# NEXUS (2026-11-21): import paths only (audioseal.libs.audiocraft.modules -> hydra.vendor.audioseal) and the
# file-level ruff exemption below (upstream style is kept as is).
# ruff: noqa

from torch import nn


class StreamableLSTM(nn.Module):
    """LSTM without worrying about the hidden state, nor the layout of the data.
    Expects input as convolutional layout.
    """

    def __init__(self, dimension: int, num_layers: int = 2, skip: bool = True):
        super().__init__()
        self.skip = skip
        self.lstm = nn.LSTM(dimension, dimension, num_layers)

    def forward(self, x):
        x = x.permute(2, 0, 1)
        y, _ = self.lstm(x)
        if self.skip:
            y = y + x
        y = y.permute(1, 2, 0)
        return y
