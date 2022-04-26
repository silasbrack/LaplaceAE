from builtins import breakpoint
import copy
from abc import abstractmethod

import torch
from torch.nn.utils import parameters_to_vector

import sys

sys.path.append("../stochman")
from stochman import nnj
import time


def swap_curr_method(curr_method, diag_out_h, pre_layer, curr_layer, post_layer):

    is_decoder_last_linear = (
        post_layer is not None
        and isinstance(curr_layer, nnj.Linear)
        and isinstance(post_layer, nnj.Reshape)
    )
    is_encoder_first_linear = (
        pre_layer is not None
        and isinstance(pre_layer, nnj.Flatten)
        and isinstance(curr_layer, nnj.Linear)
    )

    if curr_method == "approx" and is_decoder_last_linear:
        diag_out_h = False
        curr_method = "exact"

    if curr_method == "exact" and is_encoder_first_linear:
        diag_out_h = True
        curr_method = "approx"

    return curr_method, diag_out_h


def diag_structure(curr_method):

    diag_inp_m = curr_method == "approx"
    diag_out_m = curr_method in ("approx", "exact")
    diag_inp_h = curr_method == "approx"
    diag_out_h = curr_method == "approx"

    return diag_inp_m, diag_out_m, diag_inp_h, diag_out_h


class HessianCalculator:
    def __init__(self):
        super(HessianCalculator, self).__init__()
        self.device = "cuda:0" if torch.cuda.is_available() else "cpu"

    @abstractmethod
    def compute_batch(self, *args, **kwargs):
        pass

    def compute(self, loader, model, output_size):
        # keep track of running sum
        H_running_sum = None
        counter = 0

        self.feature_maps = []

        def fw_hook_get_latent(module, input, output):
            self.feature_maps.append(output.detach())

        for k in range(len(model)):
            model[k].register_forward_hook(fw_hook_get_latent)

        for batch in loader:
            H = self.compute_batch(model, output_size, *batch)
            if H_running_sum is None:
                H_running_sum = H

            if isinstance(H, list):
                H_running_sum = [h_sum + h for h_sum, h in zip(H_running_sum, H)]
            else:
                H_running_sum += H

        return H_running_sum


class MseHessianCalculator(HessianCalculator):
    def __init__(self, method):
        super(MseHessianCalculator, self).__init__()

        self.method = method  # block, exact, approx, mix

    def compute_batch(self, net, output_size, x, *args, **kwargs):
        x = x.to(self.device)

        self.feature_maps = []
        net(x)

        return self.__call__(net, self.feature_maps, x)

    def __call__(self, net, feature_maps, x, *args, **kwargs):
        # print("call")
        if x.ndim == 4:
            bs, c, h, w = x.shape
            output_size = c * h * w
        else:
            bs, output_size = x.shape

        feature_maps = [x] + feature_maps

        # if we use diagonal approximation or first layer is flatten
        tmp = torch.ones(output_size, device=x.device)
        if self.method in ("block", "exact"):
            tmp = torch.diag_embed(tmp).expand(bs, -1, -1)
        elif self.method in ("approx", "mix"):
            tmp = tmp.expand(bs, -1)

        curr_method = "approx" if self.method == "mix" else self.method
        diag_inp_m, diag_out_m, diag_inp_h, diag_out_h = diag_structure(curr_method)

        t1 = 0
        t2 = 0
        H = []
        with torch.no_grad():
            for k in range(len(net) - 1, -1, -1):

                if self.method == "mix":
                    prev_layer = net[k - 1] if k > 0 else None
                    next_layer = net[k + 1] if k < len(net) - 1 else None
                    diag_inp_m, diag_out_m, diag_inp_h, diag_out_h = diag_structure(
                        curr_method
                    )
                    curr_method, diag_out_h = swap_curr_method(
                        curr_method, diag_out_h, prev_layer, net[k], next_layer
                    )

                # jacobian w.r.t weight
                t = time.time()
                h_k = net[k]._jacobian_wrt_weight_sandwich(
                    feature_maps[k],
                    feature_maps[k + 1],
                    tmp,
                    diag_inp_m,
                    diag_out_m,
                )
                if h_k is not None:
                    H = [h_k.sum(dim=0)] + H
                t1 += time.time() - t
                t = time.time()
                # jacobian w.r.t input
                tmp = net[k]._jacobian_wrt_input_sandwich(
                    feature_maps[k],
                    feature_maps[k + 1],
                    tmp,
                    diag_inp_h,
                    diag_out_h,
                )
                t2 += time.time() - t

        # print("wrt weight", t1)
        # print("wrt input", t2)

        if self.method == "block":
            H = [H_layer for H_layer in H]
        else:
            H = torch.cat(H, dim=0)

        return H
