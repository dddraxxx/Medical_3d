# -*- coding: utf-8 -*-
import logging
from tkinter import image_names
from typing import Any, List, Tuple
from skimage import color

import torch
from torch import nn
import torch.nn.functional as F

from detectron2.structures import ImageList
from detectron2.modeling.proposal_generator import build_proposal_generator
from detectron2.modeling.backbone import build_backbone
from detectron2.modeling.meta_arch.build import META_ARCH_REGISTRY
from detectron2.structures.instances import Instances
from detectron2.structures.masks import PolygonMasks, polygons_to_bitmask
from detectron2.layers.wrappers import shapes_to_tensor

from adet.modeling.condinst.condinst3d import get_images_color_similarity_3d
from adet.modeling.condinst.dynamic_mask_head3d import (
    build_dynamic_mask_head3d,
    dice_coefficient,
)
from adet.modeling.condinst.mask_branch3d import build_mask_branch3d

from .dynamic_mask_head import build_dynamic_mask_head
from .mask_branch import build_mask_branch

from adet.utils.comm import aligned_bilinear, aligned_bilinear3d

__all__ = ["CondInst"]


logger = logging.getLogger(__name__)


def unfold_wo_center(x, kernel_size, dilation):
    assert x.dim() == 4
    assert kernel_size % 2 == 1

    # using SAME padding
    padding = (kernel_size + (dilation - 1) * (kernel_size - 1)) // 2
    unfolded_x = F.unfold(
        x, kernel_size=kernel_size, padding=padding, dilation=dilation
    )

    unfolded_x = unfolded_x.reshape(x.size(0), x.size(1), -1, x.size(2), x.size(3))

    # remove the center pixels
    size = kernel_size ** 2
    unfolded_x = torch.cat(
        (unfolded_x[:, :, : size // 2], unfolded_x[:, :, size // 2 + 1 :]), dim=2
    )

    return unfolded_x


def get_images_color_similarity(images, image_masks, kernel_size, dilation):
    assert images.dim() == 4
    assert images.size(0) == 1

    unfolded_images = unfold_wo_center(
        images, kernel_size=kernel_size, dilation=dilation
    )

    diff = images[:, :, None] - unfolded_images
    similarity = torch.exp(-torch.norm(diff, dim=1) * 0.5)

    unfolded_weights = unfold_wo_center(
        image_masks[None, None], kernel_size=kernel_size, dilation=dilation
    )
    unfolded_weights = torch.max(unfolded_weights, dim=1)[0]

    return similarity * unfolded_weights


class ImageList3D(object):
    """
    Structure that holds a list of images (of possibly
    varying sizes) as a single tensor.
    This works by padding the images to the same size.
    The original sizes of each image is stored in `image_sizes`.

    Attributes:
        image_sizes (list[tuple[int, int]]): each tuple is (h, w).
            During tracing, it becomes list[Tensor] instead.
    """

    def __init__(self, tensor: torch.Tensor, image_sizes: List[Tuple[int, int, int]]):
        """
        Arguments:
            tensor (Tensor): of shape (N, H, W) or (N, C_1, ..., C_K, H, W) where K >= 1
            image_sizes (list[tuple[int, int]]): Each tuple is (h, w). It can
                be smaller than (H, W) due to padding.
        """
        self.tensor = tensor
        self.image_sizes = image_sizes

    def __len__(self) -> int:
        return len(self.image_sizes)

    def __getitem__(self, idx) -> torch.Tensor:
        """
        Access the individual image in its original size.

        Args:
            idx: int or slice

        Returns:
            Tensor: an image of shape (H, W) or (C_1, ..., C_K, H, W) where K >= 1
        """
        size = self.image_sizes[idx]
        return self.tensor[idx, ..., : size[0], : size[1], size:[2]]

    @torch.jit.unused
    def to(self, *args: Any, **kwargs: Any) -> "ImageList":
        cast_tensor = self.tensor.to(*args, **kwargs)
        return ImageList(cast_tensor, self.image_sizes)

    @property
    def device(self) -> torch.device:
        return self.tensor.device

    @staticmethod
    def from_tensors(
        tensors: List[torch.Tensor], size_divisibility: int = 0, pad_value: float = 0.0
    ) -> "ImageList":
        """
        Args:
            tensors: a tuple or list of `torch.Tensor`, each of shape (Hi, Wi) or
                (C_1, ..., C_K, Hi, Wi) where K >= 1. The Tensors will be padded
                to the same shape with `pad_value`.
            size_divisibility (int): If `size_divisibility > 0`, add padding to ensure
                the common height and width is divisible by `size_divisibility`.
                This depends on the model and many models need a divisibility of 32.
            pad_value (float): value to pad

        Returns:
            an `ImageList`.
        """
        assert len(tensors) > 0
        assert isinstance(tensors, (tuple, list))
        for t in tensors:
            assert isinstance(t, torch.Tensor), type(t)
            assert t.shape[:-2] == tensors[0].shape[:-2], t.shape

        image_sizes = [(im.shape[-3], im.shape[-2], im.shape[-1]) for im in tensors]
        image_sizes_tensor = [shapes_to_tensor(x) for x in image_sizes]
        return ImageList(torch.stack(tensors, dim=0), image_sizes_tensor)


@META_ARCH_REGISTRY.register()
class UInst3D(nn.Module):
    """
    Main class for CondInst architectures (see https://arxiv.org/abs/2003.05664).
    """

    def __init__(self, cfg):
        super().__init__()
        self.device = torch.device(cfg.MODEL.DEVICE)

        self.backbone = build_backbone(cfg)
        self.proposal_generator = build_proposal_generator(
            cfg, self.backbone.output_shape()
        )
        self.mask_head = build_dynamic_mask_head3d(cfg)
        self.mask_branch = build_mask_branch3d(cfg, self.backbone.output_shape())

        self.mask_out_stride = cfg.MODEL.CONDINST.MASK_OUT_STRIDE

        self.max_proposals = cfg.MODEL.CONDINST.MAX_PROPOSALS
        self.topk_proposals_per_im = cfg.MODEL.CONDINST.TOPK_PROPOSALS_PER_IM

        # boxinst configs
        self.boxinst_enabled = cfg.MODEL.BOXINST.ENABLED
        self.bottom_pixels_removed = cfg.MODEL.BOXINST.BOTTOM_PIXELS_REMOVED
        self.pairwise_size = cfg.MODEL.BOXINST.PAIRWISE.SIZE
        self.pairwise_dilation = cfg.MODEL.BOXINST.PAIRWISE.DILATION
        self.pairwise_color_thresh = cfg.MODEL.BOXINST.PAIRWISE.COLOR_THRESH

        # build top module
        in_channels = self.proposal_generator.in_channels_to_top_module

        self.controller = nn.Conv3d(
            in_channels,
            self.mask_head.num_gen_params,
            kernel_size=3,
            stride=1,
            padding=1,
        )
        torch.nn.init.normal_(self.controller.weight, std=0.01)
        torch.nn.init.constant_(self.controller.bias, 0)

        # pixel_mean = torch.Tensor(cfg.MODEL.PIXEL_MEAN).to(self.device).view(3, 1, 1)
        # pixel_std = torch.Tensor(cfg.MODEL.PIXEL_STD).to(self.device).view(3, 1, 1)
        # self.normalizer = lambda x: (x - pixel_mean) / pixel_std
        self.normalize_image = cfg.MODEL.UINST3D.NORMALIZE
        # self.normalizer = lambda x: (x - x.mean(dim=[1, 2], keepdim=True)) / x.std(
        #     dim=[1, 2], keepdim=True
        # )
        self.to(self.device)

        self.only_seg = cfg.MODEL.CONDINST.ONLY_SEG

    def forward(self, batched_inputs):
        """
        x: dict[ N*C*H*W ]"""
        # print('batch_length:', len(batched_inputs))
        original_images = [x["image"].to(self.device) for x in batched_inputs]

        # normalize images
        if self.normalize_image:
            images_norm = [self.normalizer(x) for x in original_images]
        else:
            images_norm = original_images
        # print(images_norm[0].shape)
        images_norm = ImageList3D.from_tensors(
            images_norm, self.backbone.size_divisibility
        )

        # p_i to p_j
        features = self.backbone(images_norm.tensor)

        if "instances" in batched_inputs[0]:
            gt_instances = [x["instances"].to(self.device) for x in batched_inputs]
            if self.boxinst_enabled:
                original_image_masks = [
                    torch.ones_like(x[0], dtype=torch.float32) for x in original_images
                ]

                # mask out the bottom area where the COCO dataset probably has wrong annotations
                for i in range(len(original_image_masks)):
                    im_h = batched_inputs[i]["height"]
                    pixels_removed = int(
                        self.bottom_pixels_removed
                        * float(original_images[i].size(1))
                        / float(im_h)
                    )
                    if pixels_removed > 0:
                        original_image_masks[i][-pixels_removed:, :] = 0

                original_images = ImageList.from_tensors(
                    original_images, self.backbone.size_divisibility
                )
                original_image_masks = ImageList.from_tensors(
                    original_image_masks, self.backbone.size_divisibility, pad_value=0.0
                )
                self.add_bitmasks_from_boxes(
                    gt_instances,
                    original_images.tensor,
                    original_image_masks.tensor,
                    original_image_masks.tensor.size(-3),
                    original_images.tensor.size(-2),
                    original_images.tensor.size(-1),
                )
            else:
                self.add_bitmasks(
                    gt_instances,
                    images_norm.tensor.size(-3),
                    images_norm.tensor.size(-2),
                    images_norm.tensor.size(-1),
                )
        else:
            gt_instances = None
        mask_feats, sem_losses = self.mask_branch(features, gt_instances)

        if self.only_seg:
            seg = self.mask_branch.logits(self.mask_branch.seg_head(mask_feats))
            if self.training:
                losses = {}
                gt_bitmasks = torch.stack(
                    [
                        i.gt_bitmasks
                        for i in gt_instances
                    ]
                )
                seg_losses = dice_coefficient(seg.sigmoid(), gt_bitmasks)
                seg_losses = seg_losses.mean()
                losses.update(seg_losses=seg_losses)
                return losses
            else:
                results = []
                for s in seg:
                    instances = Instances([0,0])
                    instances.pred_masks = (s.sigmoid()>0.5).float()
                    results.append({"instances": instances})
                return results

        proposals, proposal_losses = self.proposal_generator(
            images_norm, features, gt_instances, self.controller
        )

        if self.training:
            mask_losses = self._forward_mask_heads_train(
                proposals, mask_feats, gt_instances
            )

            losses = {}
            losses.update(sem_losses)
            losses.update(proposal_losses)
            losses.update(mask_losses)
            # losses.pop('loss_fcos_loc')
            return losses
        else:
            pred_instances_w_masks = self._forward_mask_heads_test(
                proposals, mask_feats
            )

            padded_im_s, padded_im_h, padded_im_w = images_norm.tensor.size()[-3:]
            processed_results = []
            for im_id, (input_per_image, image_size) in enumerate(
                zip(batched_inputs, images_norm.image_sizes)
            ):
                depth = input_per_image.get("depth", image_size[0])
                height = input_per_image.get("height", image_size[1])
                width = input_per_image.get("width", image_size[2])

                instances_per_im = pred_instances_w_masks[
                    pred_instances_w_masks.im_inds == im_id
                ]
                instances_per_im = self.postprocess(
                    instances_per_im,
                    depth,
                    height,
                    width,
                    padded_im_s,
                    padded_im_h,
                    padded_im_w,
                )

                processed_results.append({"instances": instances_per_im})

            return processed_results

    def _forward_mask_heads_train(self, proposals, mask_feats, gt_instances):
        # prepare the inputs for mask heads
        pred_instances = proposals["instances"]

        assert (self.max_proposals == -1) or (
            self.topk_proposals_per_im == -1
        ), "MAX_PROPOSALS and TOPK_PROPOSALS_PER_IM cannot be used at the same time."
        if self.max_proposals != -1:
            if self.max_proposals < len(pred_instances):
                inds = torch.randperm(
                    len(pred_instances), device=mask_feats.device
                ).long()
                logger.info(
                    "clipping proposals from {} to {}".format(
                        len(pred_instances), self.max_proposals
                    )
                )
                pred_instances = pred_instances[inds[: self.max_proposals]]
        elif self.topk_proposals_per_im != -1:
            num_images = len(gt_instances)

            kept_instances = []
            for im_id in range(num_images):
                instances_per_im = pred_instances[pred_instances.im_inds == im_id]
                if len(instances_per_im) == 0:
                    kept_instances.append(instances_per_im)
                    continue

                unique_gt_inds = instances_per_im.gt_inds.unique()
                num_instances_per_gt = max(
                    int(self.topk_proposals_per_im / len(unique_gt_inds)), 1
                )

                for gt_ind in unique_gt_inds:
                    instances_per_gt = instances_per_im[
                        instances_per_im.gt_inds == gt_ind
                    ]

                    if len(instances_per_gt) > num_instances_per_gt:
                        scores = instances_per_gt.logits_pred.sigmoid().max(dim=1)[0]
                        ctrness_pred = instances_per_gt.ctrness_pred.sigmoid()
                        inds = (scores * ctrness_pred).topk(
                            k=num_instances_per_gt, dim=0
                        )[1]
                        instances_per_gt = instances_per_gt[inds]

                    kept_instances.append(instances_per_gt)

            pred_instances = Instances.cat(kept_instances)

        pred_instances.mask_head_params = pred_instances.top_feats

        loss_mask = self.mask_head(
            mask_feats, self.mask_branch.out_stride, pred_instances, gt_instances
        )

        return loss_mask

    def _forward_mask_heads_test(self, proposals, mask_feats):
        # prepare the inputs for mask heads
        for im_id, per_im in enumerate(proposals):
            per_im.im_inds = (
                per_im.locations.new_ones(len(per_im), dtype=torch.long) * im_id
            )
        pred_instances = Instances.cat(proposals)
        pred_instances.mask_head_params = pred_instances.top_feat

        pred_instances_w_masks = self.mask_head(
            mask_feats, self.mask_branch.out_stride, pred_instances
        )

        return pred_instances_w_masks

    def add_bitmasks(self, instances, im_s, im_h, im_w):
        for per_im_gt_inst in instances:
            if not per_im_gt_inst.has("gt_masks"):
                continue
            start = int(self.mask_out_stride // 2)
            bitmasks = per_im_gt_inst.get("gt_masks")  # .tensor
            s, h, w = bitmasks.size()[1:]
            # pad to new size
            bitmasks_full = F.pad(
                bitmasks,
                (
                    0,
                    im_s - s,
                    0,
                    im_h - h,
                    0,
                    im_w - w,
                ),
                "constant",
                0,
            )
            bitmasks = bitmasks_full[
                :,
                start :: self.mask_out_stride,
                start :: self.mask_out_stride,
                start :: self.mask_out_stride,
            ]
            per_im_gt_inst.gt_bitmasks = bitmasks
            per_im_gt_inst.gt_bitmasks_full = bitmasks_full

    def add_bitmasks_from_boxes(self, instances, images, image_masks, im_s, im_h, im_w):
        stride = self.mask_out_stride
        start = int(stride // 2)

        assert images.size(2) % stride == 0
        assert images.size(3) % stride == 0

        downsampled_images = F.avg_pool3d(
            images.float(), kernel_size=stride, stride=stride, padding=0
        )
        image_masks = image_masks[:, start::stride, start::stride, start::stride]
        # downsampled_images[im_i]
        for im_i, per_im_gt_inst in enumerate(instances):
            # images_lab = color.rgb2lab(downsampled_images[im_i].byte().permute(1, 2, 0).cpu().numpy())
            # images_lab = torch.as_tensor(images_lab, device=downsampled_images.device, dtype=torch.float32)
            # images_lab = images_lab.permute(2, 0, 1)[None]
            images_lab = downsampled_images[im_i][None]
            images_color_similarity = get_images_color_similarity_3d(
                images_lab,
                image_masks[im_i],
                self.pairwise_size,
                self.pairwise_dilation,
            )

            per_im_boxes = per_im_gt_inst.gt_boxes.tensor
            per_im_bitmasks = []
            per_im_bitmasks_full = []
            for per_box in per_im_boxes:
                bitmask_full = torch.zeros((im_s, im_h, im_w)).to(self.device).float()
                bitmask_full[
                    int(per_box[0]) : int(per_box[3] + 1),
                    int(per_box[1]) : int(per_box[4] + 1),
                    int(per_box[2]) : int(per_box[5] + 1),
                ] = 1.0

                bitmask = bitmask_full[start::stride, start::stride, start::stride]

                assert bitmask.size(0) * stride == im_s
                assert bitmask.size(1) * stride == im_h
                assert bitmask.size(2) * stride == im_w

                per_im_bitmasks.append(bitmask)
                per_im_bitmasks_full.append(bitmask_full)

            per_im_gt_inst.gt_bitmasks = torch.stack(per_im_bitmasks, dim=0)
            per_im_gt_inst.gt_bitmasks_full = torch.stack(per_im_bitmasks_full, dim=0)
            per_im_gt_inst.image_color_similarity = torch.cat(
                [images_color_similarity for _ in range(len(per_im_gt_inst))], dim=0
            )

    def postprocess(
        self,
        results,
        output_depth,
        output_height,
        output_width,
        padded_im_s,
        padded_im_h,
        padded_im_w,
        mask_threshold=0.5,
    ):
        """
        Resize the output instances.
        The input images are often resized when entering an object detector.
        As a result, we often need the outputs of the detector in a different
        resolution from its inputs.
        This function will resize the raw outputs of an R-CNN detector
        to produce outputs according to the desired output resolution.
        Args:
            results (Instances): the raw outputs from the detector.
                `results.image_size` contains the input image resolution the detector sees.
                This object might be modified in-place.
            output_height, output_width: the desired output resolution.
        Returns:
            Instances: the resized output from the model, based on the output resolution
        """
        scale_s, scale_x, scale_y = (
            output_depth / results.image_size[0],
            output_width / results.image_size[2],
            output_height / results.image_size[1],
        )
        resized_im_s, resized_im_h, resized_im_w = results.image_size
        results = Instances(
            (output_depth, output_height, output_width), **results.get_fields()
        )

        if results.has("pred_boxes"):
            output_boxes = results.pred_boxes
        elif results.has("proposal_boxes"):
            output_boxes = results.proposal_boxes

        output_boxes.scale(scale_s, scale_y, scale_x)
        output_boxes.clip(results.image_size)

        results = results[output_boxes.nonempty()]

        if results.has("pred_global_masks"):
            mask_s, mask_h, mask_w = results.pred_global_masks.size()[-3:]
            factor_s = padded_im_s // mask_s
            factor_h = padded_im_h // mask_h
            factor_w = padded_im_w // mask_w
            assert factor_h == factor_w == factor_s
            factor = factor_h
            pred_global_masks = aligned_bilinear3d(results.pred_global_masks, factor)
            pred_global_masks = pred_global_masks[
                :, :, :resized_im_s, :resized_im_h, :resized_im_w
            ]
            pred_global_masks = F.interpolate(
                pred_global_masks,
                size=(output_depth, output_height, output_width),
                mode="trilinear",
                align_corners=False,
            )
            pred_global_masks = pred_global_masks[:, 0, :, :, :]
            results.pred_masks = (pred_global_masks > mask_threshold).float()

        return results
