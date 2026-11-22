# MIT License
#
# Copyright (c) 2021 ShieldMnt
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# Vendored from https://github.com/ShieldMnt/invisible-watermark @ 68d0376d94a4701ed240af0841ec12e00676e325,
# file imwatermark/maxDct.py (class EmbedMaxDct, "dwtDct" method). Modifications by HYDRA NEXUS (2026-11-21):
# - only the DWT-DCT ("dwtDct") path; SVD and RivaGAN paths, pprint and unused imports removed;
# - snake_case names, mutable default arguments replaced, `decode_scores` returns the per-bit mean scores
#   (the original thresholded them inside `decode`);
# - `encode` is kept only so tests can build synthetic watermarked images; production code only decodes;
# - `decode_frame` vectorised with numpy (same block order and bit rule as the upstream loop).
"""DWT-DCT invisible watermark (ShieldMnt invisible-watermark, MIT), decoder-oriented subset."""

from __future__ import annotations

import cv2
import numpy as np
import pywt

MIN_PIXELS = 256 * 256  # the upstream encoder/decoder refuse smaller images


class EmbedMaxDct:
    def __init__(self, watermarks=None, wm_len: int = 8, scales=(0, 36, 36), block: int = 4):
        self._watermarks = list(watermarks or [])
        self._wm_len = wm_len
        self._scales = list(scales)
        self._block = block

    def encode(self, bgr: np.ndarray) -> np.ndarray:
        row, col, _ = bgr.shape
        yuv = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV)
        for channel in range(2):
            if self._scales[channel] <= 0:
                continue
            ca1, (h1, v1, d1) = pywt.dwt2(yuv[:row // 4 * 4, :col // 4 * 4, channel], "haar")
            self.encode_frame(ca1, self._scales[channel])
            yuv[:row // 4 * 4, :col // 4 * 4, channel] = pywt.idwt2((ca1, (v1, h1, d1)), "haar")
        return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR)

    def decode_scores(self, bgr: np.ndarray) -> np.ndarray:
        row, col, _ = bgr.shape
        yuv = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV)
        scores: list[list[int]] = [[] for _ in range(self._wm_len)]
        for channel in range(2):
            if self._scales[channel] <= 0:
                continue
            ca1, _ = pywt.dwt2(yuv[:row // 4 * 4, :col // 4 * 4, channel], "haar")
            scores = self.decode_frame(ca1, self._scales[channel], scores)
        return np.array([np.array(s).mean() if s else 0.0 for s in scores])

    def decode(self, bgr: np.ndarray) -> np.ndarray:
        return self.decode_scores(bgr) * 255 > 127

    def decode_frame(self, frame, scale, scores):
        # Vectorised form of the upstream per-block loop (same block order, same rule as infer_dct_matrix).
        b = self._block
        row, col = frame.shape
        nr, nc = row // b, col // b
        blocks = frame[:nr * b, :nc * b].reshape(nr, b, nc, b).transpose(0, 2, 1, 3).reshape(-1, b * b)
        pos = np.argmax(np.abs(blocks[:, 1:]), axis=1) + 1
        val = np.abs(blocks[np.arange(len(blocks)), pos])
        bits = ((val % scale) > 0.5 * scale).astype(int)
        for k in range(min(self._wm_len, len(bits))):
            scores[k].extend(bits[k::self._wm_len].tolist())
        return scores

    def diffuse_dct_matrix(self, block, wm_bit, scale):
        pos = np.argmax(abs(block.flatten()[1:])) + 1
        i, j = pos // self._block, pos % self._block
        val = block[i][j]
        if val >= 0.0:
            block[i][j] = (val // scale + 0.25 + 0.5 * wm_bit) * scale
        else:
            val = abs(val)
            block[i][j] = -1.0 * (val // scale + 0.25 + 0.5 * wm_bit) * scale
        return block

    def infer_dct_matrix(self, block, scale):
        pos = np.argmax(abs(block.flatten()[1:])) + 1
        i, j = pos // self._block, pos % self._block
        val = block[i][j]
        if val < 0:
            val = abs(val)
        return 1 if (val % scale) > 0.5 * scale else 0

    def encode_frame(self, frame, scale):
        row, col = frame.shape
        num = 0
        for i in range(row // self._block):
            for j in range(col // self._block):
                block = frame[i * self._block: i * self._block + self._block,
                              j * self._block: j * self._block + self._block]
                wm_bit = self._watermarks[num % self._wm_len]
                frame[i * self._block: i * self._block + self._block,
                      j * self._block: j * self._block + self._block] = self.diffuse_dct_matrix(block, wm_bit, scale)
                num += 1


def bits_of_bytes(content: bytes) -> list[int]:
    return [int(b) for b in np.unpackbits(np.frombuffer(content, dtype=np.uint8))]


def encode_bits(bgr: np.ndarray, bits: list[int]) -> np.ndarray:
    """Test helper: embed `bits` like WatermarkEncoder.set_by_bits + encode(method='dwtDct')."""
    return EmbedMaxDct([int(b) % 2 for b in bits], wm_len=len(bits)).encode(bgr)


def decode_bits(bgr: np.ndarray, length: int) -> np.ndarray:
    """Like WatermarkDecoder('bits', length).decode(bgr, 'dwtDct'): bool array of `length` bits."""
    if bgr.shape[0] * bgr.shape[1] < MIN_PIXELS:
        raise ValueError("image too small, should be larger than 256x256")
    return EmbedMaxDct(wm_len=length).decode(bgr)
