import logging
import torch
from torch import nn
import torch.nn.functional as F

from detectron2.layers import cat
from detectron2.structures import Instances, Boxes
from detectron2.utils.comm import get_world_size
from fvcore.nn import sigmoid_focal_loss_jit
from adet.modeling.backbone.unet3d import compute_ctrness_targets_3d

from adet.utils.comm import compute_ious_3d, reduce_sum, reduce_mean, compute_ious
from adet.layers import ml_nms3d, IOULoss
from adet.utils.dataset_3d import Boxes3D


logger = logging.getLogger(__name__)

INF = 100000000

"""
Shape shorthand in this module:

    N: number of images in the minibatch
    L: number of feature maps per image on which RPN is run
    Hi, Wi: height and width of the i-th feature map
    4: size of the box parameterization

Naming convention:

    labels: refers to the ground-truth class of an position.

    reg_targets: refers to the 4-d (left, top, right, bottom) distances that parameterize the ground-truth box.

    logits_pred: predicted classification scores in [-inf, +inf];
    
    reg_pred: the predicted (left, top, right, bottom), corresponding to reg_targets 

    ctrness_pred: predicted centerness scores

"""


def compute_ctrness_targets(reg_targets):
    if len(reg_targets) == 0:
        return reg_targets.new_zeros(len(reg_targets))
    left_right = reg_targets[:, [0, 2]]
    top_bottom = reg_targets[:, [1, 3]]
    ctrness = (left_right.min(dim=-1)[0] / left_right.max(dim=-1)[0]) * (
        top_bottom.min(dim=-1)[0] / top_bottom.max(dim=-1)[0]
    )
    return torch.sqrt(ctrness)


class FCOSOutputs3D(nn.Module):
    def __init__(self, cfg):
        super().__init__()

        self.focal_loss_alpha = cfg.MODEL.FCOS.LOSS_ALPHA
        self.focal_loss_gamma = cfg.MODEL.FCOS.LOSS_GAMMA
        self.center_sample = cfg.MODEL.FCOS.CENTER_SAMPLE
        self.radius = cfg.MODEL.FCOS.POS_RADIUS
        self.pre_nms_thresh_train = cfg.MODEL.FCOS.INFERENCE_TH_TRAIN
        self.pre_nms_topk_train = cfg.MODEL.FCOS.PRE_NMS_TOPK_TRAIN
        self.post_nms_topk_train = cfg.MODEL.FCOS.POST_NMS_TOPK_TRAIN
        self.loc_loss_func = IOULoss(cfg.MODEL.FCOS.LOC_LOSS_TYPE)

        self.pre_nms_thresh_test = cfg.MODEL.FCOS.INFERENCE_TH_TEST
        self.pre_nms_topk_test = cfg.MODEL.FCOS.PRE_NMS_TOPK_TEST
        self.post_nms_topk_test = cfg.MODEL.FCOS.POST_NMS_TOPK_TEST
        self.nms_thresh = cfg.MODEL.FCOS.NMS_TH
        self.thresh_with_ctr = cfg.MODEL.FCOS.THRESH_WITH_CTR
        self.box_quality = cfg.MODEL.FCOS.BOX_QUALITY

        self.num_classes = cfg.MODEL.FCOS.NUM_CLASSES
        self.strides = cfg.MODEL.FCOS.FPN_STRIDES

        # generate sizes of interest
        soi = []
        prev_size = -1
        for s in cfg.MODEL.FCOS.SIZES_OF_INTEREST:
            soi.append([prev_size, s])
            prev_size = s
        soi.append([prev_size, INF])
        self.sizes_of_interest = soi

        self.loss_normalizer_cls = cfg.MODEL.FCOS.LOSS_NORMALIZER_CLS
        assert self.loss_normalizer_cls in (
            "moving_fg",
            "fg",
            "all",
        ), 'MODEL.FCOS.CLS_LOSS_NORMALIZER can only be "moving_fg", "fg", or "all"'

        # For an explanation, please refer to
        # https://github.com/facebookresearch/detectron2/blob/ea8b17914fc9a5b7d82a46ccc72e7cf6272b40e4/detectron2/modeling/meta_arch/retinanet.py#L148
        self.moving_num_fg = (
            100  # initialize with any reasonable #fg that's not too small
        )
        self.moving_num_fg_momentum = 0.9

        self.loss_weight_cls = cfg.MODEL.FCOS.LOSS_WEIGHT_CLS

        self.iter = 0

    def _transpose(self, training_targets, num_loc_list):
        """
        This function is used to transpose image first training targets to level first ones
        :return: level first training targets
        """
        for im_i in range(len(training_targets)):
            training_targets[im_i] = torch.split(
                training_targets[im_i], num_loc_list, dim=0
            )

        targets_level_first = []
        for targets_per_level in zip(*training_targets):
            targets_level_first.append(torch.cat(targets_per_level, dim=0))
        return targets_level_first

    def _get_ground_truth(self, locations, gt_instances):
        num_loc_list = [len(loc) for loc in locations]

        # compute locations to size ranges
        loc_to_size_range = []
        for l, loc_per_level in enumerate(locations):
            loc_to_size_range_per_level = loc_per_level.new_tensor(
                self.sizes_of_interest[l]
            )
            loc_to_size_range.append(
                loc_to_size_range_per_level[None].expand(num_loc_list[l], -1)
            )

        loc_to_size_range = torch.cat(loc_to_size_range, dim=0)
        locations = torch.cat(locations, dim=0)

        training_targets = self.compute_targets_for_locations(
            locations, gt_instances, loc_to_size_range, num_loc_list
        )

        training_targets["locations"] = [
            locations.clone() for _ in range(len(gt_instances))
        ]
        training_targets["im_inds"] = [
            locations.new_ones(locations.size(0), dtype=torch.long) * i
            for i in range(len(gt_instances))
        ]

        # transpose im first training_targets to level first ones
        training_targets = {
            k: self._transpose(v, num_loc_list) for k, v in training_targets.items()
        }

        training_targets["fpn_levels"] = [
            loc.new_ones(len(loc), dtype=torch.long) * level
            for level, loc in enumerate(training_targets["locations"])
        ]

        # we normalize reg_targets by FPN's strides here
        reg_targets = training_targets["reg_targets"]
        for l in range(len(reg_targets)):
            reg_targets[l] = reg_targets[l] / float(self.strides[l])

        return training_targets

    def get_sample_region(
        self, boxes, strides, num_loc_list, loc_xs, loc_ys, loc_zs, bitmasks=None, radius=1
    ):
        '''
        Here, (x,y,z) corresponds to (s,h,w)'''
        if bitmasks is not None:
            _, s, h, w = bitmasks.size()

            zs = torch.arange(0, s, dtype=torch.float32, device=bitmasks.device)
            ys = torch.arange(0, h, dtype=torch.float32, device=bitmasks.device)
            xs = torch.arange(0, w, dtype=torch.float32, device=bitmasks.device)

            m00 = bitmasks.sum(dim=(-1,-2,-3)).clamp(min=1e-6)
            m10 = (bitmasks * xs).sum(dim=(-1,-2,-3))
            m01 = (bitmasks * ys[:, None]).sum(dim=(-1,-2,-3))
            m11 = (bitmasks*zs[:,None,None]).sum(dim=(-1,-2,-3))
            center_z = m10 / m00
            center_y = m01 / m00
            center_x = m11 / m00
        else:
            center_x = boxes[..., [0, 2]].sum(dim=-1) * 0.5
            center_y = boxes[..., [1, 3]].sum(dim=-1) * 0.5
            raise Exception()

        num_gts = boxes.shape[0]
        K = len(loc_xs)
        boxes = boxes[None].expand(K, num_gts, -1)
        center_x = center_x[None].expand(K, num_gts)
        center_y = center_y[None].expand(K, num_gts)
        center_z = center_z[None].expand(K,num_gts)
        center_gt = boxes.new_zeros(boxes.shape)
        # no gt
        if center_x.numel() == 0 or center_x[..., 0].sum() == 0:
            return loc_xs.new_zeros(loc_xs.shape, dtype=torch.uint8)
        beg = 0
        for level, num_loc in enumerate(num_loc_list):
            end = beg + num_loc
            stride = strides[level] * radius
            xmin = center_x[beg:end] - stride
            ymin = center_y[beg:end] - stride
            zmin = center_z[beg:end] - stride
            xmax = center_x[beg:end] + stride
            ymax = center_y[beg:end] + stride
            zmax = center_z[beg:end] + stride
            # limit sample region in gt
            center_gt[beg:end, :, 0] = torch.where(
                xmin > boxes[beg:end, :, 0], xmin, boxes[beg:end, :, 0]
            )
            center_gt[beg:end, :, 1] = torch.where(
                ymin > boxes[beg:end, :, 1], ymin, boxes[beg:end, :, 1]
            )
            center_gt[beg:end,:,2] = torch.max(boxes[beg:end,:,2], zmin)
            center_gt[beg:end, :, 3] = torch.where(
                xmax > boxes[beg:end, :, 3], boxes[beg:end, :, 3], xmax
            )
            center_gt[beg:end, :, 4] = torch.where(
                ymax > boxes[beg:end, :, 4], boxes[beg:end, :, 4], ymax
            )
            center_gt[beg:end,:,5] = torch.min(boxes[beg:end,:,5], zmax)

            beg = end
        left = loc_xs[:, None] - center_gt[..., 0]
        right = center_gt[..., 3] - loc_xs[:, None]
        top = loc_ys[:, None] - center_gt[..., 1]
        bottom = center_gt[..., 4] - loc_ys[:, None]
        near = loc_zs[:, None] - center_gt[...,2]
        far  = center_gt[...,5]  - loc_zs[:,None]
        center_bbox = torch.stack((left, top, near, right, bottom, far), -1)
        inside_gt_bbox_mask = center_bbox.min(-1)[0] > 0
        return inside_gt_bbox_mask

    def compute_targets_for_locations(
        self, locations, targets, size_ranges, num_loc_list
    ):
        labels = []
        reg_targets = []
        target_inds = []
        xs, ys, zs = locations[:, 0], locations[:, 1], locations[:, 2]

        num_targets = 0
        for im_i in range(len(targets)):
            targets_per_im = targets[im_i]
            bboxes = targets_per_im.gt_boxes.tensor
            labels_per_im = targets_per_im.gt_classes

            # no gt
            if bboxes.numel() == 0:
                labels.append(
                    labels_per_im.new_zeros(locations.size(0)) + self.num_classes
                )
                reg_targets.append(locations.new_zeros((locations.size(0), 6)))
                target_inds.append(labels_per_im.new_zeros(locations.size(0)) - 1)
                continue

            area = targets_per_im.gt_boxes.area()

            l = xs[:, None] - bboxes[:, 0][None]
            t = ys[:, None] - bboxes[:, 1][None]
            n = zs[:, None] - bboxes[:, 2][None]
            r = bboxes[:, 3][None] - xs[:, None]
            b = bboxes[:, 4][None] - ys[:, None]
            f = bboxes[:, 5][None] - zs[:, None]
            reg_targets_per_im = torch.stack([l, t, n, r, b, f], dim=2)

            if self.center_sample:
                if targets_per_im.has("gt_bitmasks_full"):
                    bitmasks = targets_per_im.gt_bitmasks_full
                else:
                    bitmasks = None
                is_in_boxes = self.get_sample_region(
                    bboxes,
                    self.strides,
                    num_loc_list,
                    xs,
                    ys,
                    zs,
                    bitmasks=bitmasks,
                    radius=self.radius,
                )
            else:
                is_in_boxes = reg_targets_per_im.min(dim=2)[0] > 0

            max_reg_targets_per_im = reg_targets_per_im.max(dim=2)[0]
            # limit the regression range for each location
            is_cared_in_the_level = (max_reg_targets_per_im >= size_ranges[:, [0]]) & (
                max_reg_targets_per_im <= size_ranges[:, [1]]
            )

            locations_to_gt_area = area[None].repeat(len(locations), 1)
            locations_to_gt_area[is_in_boxes == 0] = INF
            locations_to_gt_area[is_cared_in_the_level == 0] = INF

            # if there are still more than one objects for a location,
            # we choose the one with minimal area
            locations_to_min_area, locations_to_gt_inds = locations_to_gt_area.min(
                dim=1
            )

            reg_targets_per_im = reg_targets_per_im[
                range(len(locations)), locations_to_gt_inds
            ]
            target_inds_per_im = locations_to_gt_inds + num_targets
            num_targets += len(targets_per_im)

            labels_per_im = labels_per_im[locations_to_gt_inds]
            labels_per_im[locations_to_min_area == INF] = self.num_classes

            labels.append(labels_per_im)
            reg_targets.append(reg_targets_per_im)
            target_inds.append(target_inds_per_im)

        return {
            "labels": labels,
            "reg_targets": reg_targets,
            "target_inds": target_inds,
        }

    def losses(
        self,
        logits_pred,
        reg_pred,
        ctrness_pred,
        locations,
        gt_instances,
        top_feats=None,
    ):
        """
        Return the losses from a set of FCOS predictions and their associated ground-truth.

        Returns:
            dict[loss name -> loss value]: A dict mapping from loss name to loss value.
        """
        training_targets = self._get_ground_truth(locations, gt_instances)

        # Collect all logits and regression predictions over feature maps
        # and images to arrive at the same shape as the labels and targets
        # The final ordering is L, N, H, W from slowest to fastest axis.

        instances = Instances((0, 0))
        instances.labels = cat(
            [
                # Reshape: (N, 1, Si, Hi, Wi) -> (N*Si*Hi*Wi,)
                x.reshape(-1)
                for x in training_targets["labels"]
            ],
            dim=0,
        )
        instances.gt_inds = cat(
            [
                # Reshape: (N, 1, Si, Hi, Wi) -> (N*Si*Hi*Wi,)
                x.reshape(-1)
                for x in training_targets["target_inds"]
            ],
            dim=0,
        )
        instances.im_inds = cat(
            [x.reshape(-1) for x in training_targets["im_inds"]], dim=0
        )
        instances.reg_targets = cat(
            [
                # Reshape: (N, Si, Hi, Wi, 6) -> (N*Si*Hi*Wi, 6)
                x.reshape(-1, 6)
                for x in training_targets["reg_targets"]
            ],
            dim=0,
        )
        instances.locations = cat(
            [x.reshape(-1, 3) for x in training_targets["locations"]], dim=0
        )
        instances.fpn_levels = cat(
            [x.reshape(-1) for x in training_targets["fpn_levels"]], dim=0
        )

        instances.logits_pred = cat(
            [
                # Reshape: (N, C, Si, Hi, Wi) -> (N, Si, Hi, Wi, C) -> (N*Si*Hi*Wi, C)
                x.permute(0, 2, 3, 4, 1).reshape(-1, self.num_classes)
                for x in logits_pred
            ],
            dim=0,
        )
        instances.reg_pred = cat(
            [
                # Reshape: (N, B, Hi, Wi) -> (N, Si, Hi, Wi, B) -> (N*Si*Hi*Wi, B)
                x.permute(0, 2, 3, 4, 1).reshape(-1, 6)
                for x in reg_pred
            ],
            dim=0,
        )
        instances.ctrness_pred = cat(
            [
                # Reshape: (N, 1, Si, Hi, Wi) -> (N*Si*Hi*Wi,)
                x.permute(0, 2, 3, 4, 1).reshape(-1)
                for x in ctrness_pred
            ],
            dim=0,
        )

        if len(top_feats) > 0:
            instances.top_feats = cat(
                [
                    # Reshape: (N, -1, Hi, Wi) -> (N*Hi*Wi, -1)
                    x.permute(0, 2, 3, 4, 1).reshape(-1, x.size(1))
                    for x in top_feats
                ],
                dim=0,
            )

        return self.fcos_losses(instances)

    def fcos_losses(self, instances):
        losses, extras = {}, {}

        assert instances.reg_pred.shape == instances.reg_targets.shape

        # 1. compute the cls loss
        num_classes = instances.logits_pred.size(1)
        assert num_classes == self.num_classes

        labels = instances.labels.flatten()

        pos_inds = torch.nonzero(labels != num_classes).squeeze(1)

        num_pos_local = torch.ones_like(pos_inds).sum()
        num_pos_avg = max(reduce_mean(num_pos_local).item(), 1.0)

        # prepare one_hot
        class_target = torch.zeros_like(instances.logits_pred)
        class_target[pos_inds, labels[pos_inds]] = 1

        class_loss = sigmoid_focal_loss_jit(
            instances.logits_pred,
            class_target,
            alpha=self.focal_loss_alpha,
            gamma=self.focal_loss_gamma,
            reduction="sum",
        )

        if self.loss_normalizer_cls == "moving_fg":
            self.moving_num_fg = (
                self.moving_num_fg_momentum * self.moving_num_fg
                + (1 - self.moving_num_fg_momentum) * num_pos_avg
            )
            class_loss = class_loss / self.moving_num_fg
        elif self.loss_normalizer_cls == "fg":
            class_loss = class_loss / num_pos_avg
        else:
            num_samples_local = torch.ones_like(labels).sum()
            num_samples_avg = max(reduce_mean(num_samples_local).item(), 1.0)
            class_loss = class_loss / num_samples_avg

        losses["loss_fcos_cls"] = class_loss * self.loss_weight_cls

        # 2. compute the box regression and quality loss
        print('total inds: {}'.format(instances.gt_inds.unique()))
        instances = instances[pos_inds]
        instances.pos_inds = pos_inds
        print('it contains gt_inds {}'.format(instances.gt_inds))
        print('from fcos_output3d, positve logits_pred {}'.format(instances.logits_pred[:10].detach().cpu().numpy()[:, 0]))


        ious, gious = compute_ious_3d(instances.reg_pred, instances.reg_targets)
        print('from fcos_output3d, reg_pred is {}'.format(instances.reg_pred[:10].detach().cpu().numpy()))

        if self.box_quality == "ctrness":
            ctrness_targets = compute_ctrness_targets_3d(instances.reg_targets)
            instances.gt_ctrs = ctrness_targets

            # filter centerness_targets
            # ctr_thres = 1 - self.iter/200
            # ctr_inds = instances.gt_ctrs > ctr_thres
            # instances = instances[ctr_inds]
            # ctrness_targets = 
            print('from fcos_output3d, ctrness_target is {}'.format(ctrness_targets.sort(descending=True)[0].cpu().numpy()))

            # ious, gious = ious[ctrness_targets>0.7], gious[ctrness_targets>0.7]
            # wctrness = ctrness_targets[ctrness_targets>0.7]
            # ctrness_targets_sum = wctrness.sum()
            ctrness_targets_sum = ctrness_targets.sum()
            loss_denorm = max(reduce_mean(ctrness_targets_sum).item(), 1e-6)
            extras["loss_denorm"] = loss_denorm

            # print(ious.shape, gious.shape, wctrness.shape)
            # reg_loss = self.loc_loss_func(ious, gious, wctrness) / loss_denorm
            reg_loss = self.loc_loss_func(ious, gious, ctrness_targets) / loss_denorm
            losses["loss_fcos_loc"] = reg_loss

            ctrness_loss = (
                F.binary_cross_entropy_with_logits(
                    instances.ctrness_pred, ctrness_targets, reduction="sum"
                )
                / num_pos_avg
            )
            losses["loss_fcos_ctr"] = ctrness_loss
        elif self.box_quality == "iou":
            reg_loss = self.loc_loss_func(ious, gious) / num_pos_avg
            losses["loss_fcos_loc"] = reg_loss

            print('from fcos_output3d, iou_target is {}'.format(ious.detach().cpu().numpy()))

            quality_loss = (
                F.binary_cross_entropy_with_logits(
                    instances.ctrness_pred, ious.detach(), reduction="sum"
                )
                / num_pos_avg
            )
            losses["loss_fcos_iou"] = quality_loss
        else:
            raise NotImplementedError

        extras["instances"] = instances

        return extras, losses

    def predict_proposals(
        self,
        logits_pred,
        reg_pred,
        ctrness_pred,
        locations,
        image_sizes,
        top_feats=None,
    ):
        if self.training:
            self.pre_nms_thresh = self.pre_nms_thresh_train
            self.pre_nms_topk = self.pre_nms_topk_train
            self.post_nms_topk = self.post_nms_topk_train
        else:
            self.pre_nms_thresh = self.pre_nms_thresh_test
            self.pre_nms_topk = self.pre_nms_topk_test
            self.post_nms_topk = self.post_nms_topk_test

        sampled_boxes = []

        bundle = {
            "l": locations,
            "o": logits_pred,
            "r": reg_pred,
            "c": ctrness_pred,
            "s": self.strides,
        }

        if len(top_feats) > 0:
            bundle["t"] = top_feats

        for i, per_bundle in enumerate(zip(*bundle.values())):
            # get per-level bundle
            per_bundle = dict(zip(bundle.keys(), per_bundle))
            # recall that during training, we normalize regression targets with FPN's stride.
            # we denormalize them here.
            l = per_bundle["l"]
            o = per_bundle["o"]
            r = per_bundle["r"] * per_bundle["s"]
            c = per_bundle["c"]
            t = per_bundle["t"] if "t" in bundle else None

            sampled_boxes.append(
                self.forward_for_single_feature_map(l, o, r, c, image_sizes, t)
            )

            for per_im_sampled_boxes in sampled_boxes[-1]:
                per_im_sampled_boxes.fpn_levels = (
                    l.new_ones(len(per_im_sampled_boxes), dtype=torch.long) * i
                )

        boxlists = list(zip(*sampled_boxes))
        boxlists = [Instances.cat(boxlist) for boxlist in boxlists]
        boxlists = self.select_over_all_levels(boxlists)

        return boxlists

    def forward_for_single_feature_map(
        self, locations, logits_pred, reg_pred, ctrness_pred, image_sizes, top_feat=None
    ):
        N, C, S, H, W = logits_pred.shape

        # put in the same format as locations
        logits_pred = logits_pred.view(N, C, S, H, W).permute(0, 2, 3, 4, 1)
        logits_pred = logits_pred.reshape(N, -1, C).sigmoid()
        box_regression = reg_pred.view(N, 6, S, H, W).permute(0, 2, 3, 4, 1)
        box_regression = box_regression.reshape(N, -1, 6)
        ctrness_pred = ctrness_pred.view(N, 1, S, H, W).permute(0, 2, 3, 4, 1)
        ctrness_pred = ctrness_pred.reshape(N, -1).sigmoid()
        if top_feat is not None:
            top_feat = top_feat.view(N, -1, S, H, W).permute(0, 2, 3, 4, 1)
            top_feat = top_feat.reshape(N, S * H * W, -1)

        # if self.thresh_with_ctr is True, we multiply the classification
        # scores with centerness scores before applying the threshold.
        if self.thresh_with_ctr:
            logits_pred = logits_pred * ctrness_pred[:, :, None]
        candidate_inds = logits_pred > self.pre_nms_thresh
        pre_nms_top_n = candidate_inds.reshape(N, -1).sum(1)
        pre_nms_top_n = pre_nms_top_n.clamp(max=self.pre_nms_topk)

        if not self.thresh_with_ctr:
            logits_pred = logits_pred * ctrness_pred[:, :, None]

        results = []
        for i in range(N):
            per_box_cls = logits_pred[i]
            per_candidate_inds = candidate_inds[i]
            per_box_cls = per_box_cls[per_candidate_inds]

            per_candidate_nonzeros = per_candidate_inds.nonzero()
            per_box_loc = per_candidate_nonzeros[:, 0]
            per_class = per_candidate_nonzeros[:, 1]

            per_box_regression = box_regression[i]
            per_box_regression = per_box_regression[per_box_loc]
            per_locations = locations[per_box_loc]
            if top_feat is not None:
                per_top_feat = top_feat[i]
                per_top_feat = per_top_feat[per_box_loc]

            per_pre_nms_top_n = pre_nms_top_n[i]

            if per_candidate_inds.sum().item() > per_pre_nms_top_n.item():
                per_box_cls, top_k_indices = per_box_cls.topk(
                    per_pre_nms_top_n, sorted=False
                )
                per_class = per_class[top_k_indices]
                per_box_regression = per_box_regression[top_k_indices]
                per_locations = per_locations[top_k_indices]
                if top_feat is not None:
                    per_top_feat = per_top_feat[top_k_indices]

            detections = torch.stack(
                [
                    per_locations[:, 0] - per_box_regression[:, 0],
                    per_locations[:, 1] - per_box_regression[:, 1],
                    per_locations[:, 2] - per_box_regression[:, 2],
                    per_locations[:, 0] + per_box_regression[:, 3],
                    per_locations[:, 1] + per_box_regression[:, 4],
                    per_locations[:, 2] + per_box_regression[:, 5],
                ],
                dim=1,
            )

            boxlist = Instances(image_sizes[i])
            boxlist.pred_boxes = Boxes3D(detections)
            boxlist.scores = torch.sqrt(per_box_cls)
            boxlist.pred_classes = per_class
            boxlist.locations = per_locations
            if top_feat is not None:
                boxlist.top_feat = per_top_feat
            results.append(boxlist)

        return results

    def select_over_all_levels(self, boxlists):
        num_images = len(boxlists)
        results = []
        for i in range(num_images):
            # multiclass nms
            result = ml_nms3d(boxlists[i], self.nms_thresh)
            # result = boxlists[i]
            number_of_detections = len(result)

            # Limit to max_per_image detections **over all classes**
            if number_of_detections > self.post_nms_topk > 0:
                cls_scores = result.scores
                image_thresh, _ = torch.kthvalue(
                    cls_scores.cpu(), number_of_detections - self.post_nms_topk + 1
                )
                keep = cls_scores >= image_thresh.item()
                keep = torch.nonzero(keep).squeeze(1)
                result = result[keep]
            results.append(result)
        return results
