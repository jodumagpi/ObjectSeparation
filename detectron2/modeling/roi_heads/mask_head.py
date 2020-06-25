# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
from typing import Dict, List
import fvcore.nn.weight_init as weight_init
import torch
from torch import nn
from torch.nn import functional as F

from detectron2.layers import Conv2d, ConvTranspose2d, ShapeSpec, cat, get_norm
from detectron2.structures import Instances
from detectron2.utils.events import get_event_storage
from detectron2.utils.registry import Registry
from detectron2.structures.masks import PolygonMasks
from itertools import combinations

import numpy as np
import cv2 as cv
import pickle

ROI_MASK_HEAD_REGISTRY = Registry("ROI_MASK_HEAD")
ROI_MASK_HEAD_REGISTRY.__doc__ = """
Registry for mask heads, which predicts instance masks given
per-region features.

The registered object will be called with `obj(cfg, input_shape)`.
"""


def mask_rcnn_loss(pred_mask_logits, instances, vis_period=0):
    """
    Compute the mask prediction loss defined in the Mask R-CNN paper.

    Args:
        pred_mask_logits (Tensor): A tensor of shape (B, C, Hmask, Wmask) or (B, 1, Hmask, Wmask)
            for class-specific or class-agnostic, where B is the total number of predicted masks
            in all images, C is the number of foreground classes, and Hmask, Wmask are the height
            and width of the mask predictions. The values are logits.
        instances (list[Instances]): A list of N Instances, where N is the number of images
            in the batch. These instances are in 1:1
            correspondence with the pred_mask_logits. The ground-truth labels (class, box, mask,
            ...) associated with each instance are stored in fields.
        vis_period (int): the period (in steps) to dump visualization.

    Returns:
        mask_loss (Tensor): A scalar tensor containing the loss.
    """
    cls_agnostic_mask = pred_mask_logits.size(1) == 1
    total_num_masks = pred_mask_logits.size(0)
    mask_side_len = pred_mask_logits.size(2)
    assert pred_mask_logits.size(2) == pred_mask_logits.size(3), "Mask prediction must be square!"

    EDGE_WEIGHT = 2
    storage = get_event_storage()
    if storage.iter % 1000 == 0:
        print('Edge weight:{}'.format(EDGE_WEIGHT))

    gt_classes = []
    weights = []
    gt_masks = []
    overlap = []
    kernel = np.ones((3, 3),np.uint8)
    roi_weights = []
    for instances_per_image in instances:
        # search for each ground truth mask for each instance
        unq_gt_msk = []
        unq_gt_msk_tensor = []
        for i, mask in enumerate(instances_per_image.gt_masks):
            if list(mask[0]) not in unq_gt_msk:
                unq_gt_msk.append(list(mask[0]))
                unq_gt_msk_tensor.append(mask[0])
        
        # create a volume of the ground truth mask for each ground truth box
        per_ins_msk = []
        for mask in unq_gt_msk_tensor:
            temp_msk = []
            for i in range(len(instances_per_image.gt_masks)):
                temp_msk.append([mask])
            per_ins_msk.append(PolygonMasks(temp_msk).crop_and_resize(
                        instances_per_image.proposal_boxes.tensor, mask_side_len
                        ).to(dtype=torch.float32, device=pred_mask_logits.device))

        # search for unique ROIs 
        gt_boxes = np.asarray([np.asarray(x.cpu()) for x in instances_per_image.gt_boxes])
        unique_gt_boxes, roi_counts = np.unique(gt_boxes, axis=0, return_counts=True)
        roi_counts = torch.from_numpy(roi_counts)

        # find the ROIs that are the closest to the ground truth labels
        close_iou_idx = []
        for i in range(len(unique_gt_boxes)):
            best_iou = 0
            best_box = 0
            for j, box in enumerate(instances_per_image.proposal_boxes):
                if bb_intersection_over_union(unique_gt_boxes[i], box.detach().cpu().numpy()) > best_iou:
                    best_iou = bb_intersection_over_union(unique_gt_boxes[i], box.detach().cpu().numpy())
                    best_box = j
            close_iou_idx.append(best_box)

        # create weigthts for ROIs   
        per_instance_roi = torch.ones((len(instances_per_image), 1, 1))
        for p, i in enumerate(close_iou_idx):
            per_instance_roi[i] = roi_counts[p]
        roi_weights.append(per_instance_roi)

        # get the real masks
        if len(instances_per_image) == 0:
            continue
        if not cls_agnostic_mask:
            gt_classes_per_image = instances_per_image.gt_classes.to(dtype=torch.int64)
            gt_classes.append(gt_classes_per_image)

        gt_masks_per_image = instances_per_image.gt_masks.crop_and_resize(
            instances_per_image.proposal_boxes.tensor, mask_side_len
        ).to(device=pred_mask_logits.device)

        gt_masks.append(gt_masks_per_image)

		# find the overlapped areas
        if len(per_ins_msk) > 1: # there is a possibility of overlap
            combs = combinations(per_ins_msk, 2) # pair up
            temp = []
            for c in combs:
                temp.append(c[0]*c[1])
            per_ins_overlap_masks = sum(temp)
            per_ins_overlap_masks = per_ins_overlap_masks * gt_masks_per_image.to(dtype=torch.float32)
        else:
            per_ins_overlap_masks = torch.zeros(gt_masks_per_image.shape)
        
        overlap.append(per_ins_overlap_masks.to(device=pred_mask_logits.device))

        # for per edge weights
        for masks in gt_masks_per_image.detach().cpu().numpy():
            bg = np.zeros((mask_side_len, mask_side_len))
            cnts, _ = cv.findContours(np.where(masks == True, 255, 0).astype(np.uint8), cv.RETR_EXTERNAL, cv.CHAIN_APPROX_SIMPLE)
            cv.drawContours(bg, cnts, -1, 255, -1)
            dilation = cv.dilate(bg, kernel).astype(np.uint8)
            erosion = cv.erode(bg, kernel).astype(np.uint8)
            edges = np.asarray(np.where(np.bitwise_xor(dilation, erosion) == 255, True, False))
            weights.append(torch.from_numpy(edges).unsqueeze(0))
                
    if len(gt_masks) == 0:
        return pred_mask_logits.sum() * 0

    gt_masks = cat(gt_masks, dim=0)
	
    overlap = cat(overlap, dim=0)
    overlap = quad(overlap).to(dtype=torch.float32, device=pred_mask_logits.device)
	
    weights = cat(weights, dim=0)
    weights = torch.from_numpy(np.where(weights == False, 1, EDGE_WEIGHT)).to(dtype=torch.float32, device=pred_mask_logits.device)
	
    roi_weights = cat(roi_weights, dim=0).to(dtype=torch.float32,device=pred_mask_logits.device)
    
    weights = overlap + weights + roi_weights

    if cls_agnostic_mask:
        pred_mask_logits = pred_mask_logits[:, 0]
    else:
        indices = torch.arange(total_num_masks)
        gt_classes = cat(gt_classes, dim=0)
        pred_mask_logits = pred_mask_logits[indices, gt_classes]

    if gt_masks.dtype == torch.bool:
        gt_masks_bool = gt_masks
    else:
        # Here we allow gt_masks to be float as well (depend on the implementation of rasterize())
        gt_masks_bool = gt_masks > 0.5
    gt_masks = gt_masks.to(dtype=torch.float32)

    # Log the training accuracy (using gt classes and 0.5 threshold)
    mask_incorrect = (pred_mask_logits > 0.0) != gt_masks_bool
    mask_accuracy = 1 - (mask_incorrect.sum().item() / max(mask_incorrect.numel(), 1.0))
    num_positive = gt_masks_bool.sum().item()
    false_positive = (mask_incorrect & ~gt_masks_bool).sum().item() / max(
        gt_masks_bool.numel() - num_positive, 1.0
    )
    false_negative = (mask_incorrect & gt_masks_bool).sum().item() / max(num_positive, 1.0)

    # Visualization (default: disabled)
    storage.put_scalar("mask_rcnn/accuracy", mask_accuracy)
    storage.put_scalar("mask_rcnn/false_positive", false_positive)
    storage.put_scalar("mask_rcnn/false_negative", false_negative)
    if vis_period > 0 and storage.iter % vis_period == 0:
        pred_masks = pred_mask_logits.sigmoid()
        vis_masks = torch.cat([pred_masks, gt_masks], axis=2)
        name = "Left: mask prediction;   Right: mask GT"
        for idx, vis_mask in enumerate(vis_masks):
            vis_mask = torch.stack([vis_mask] * 3, axis=0)
            storage.put_image(name + f" ({idx})", vis_mask)
            
    mask_loss = F.binary_cross_entropy_with_logits(pred_mask_logits, gt_masks, weight=weights, reduction="mean")
    return mask_loss


def mask_rcnn_inference(pred_mask_logits, pred_instances):
    """
    Convert pred_mask_logits to estimated foreground probability masks while also
    extracting only the masks for the predicted classes in pred_instances. For each
    predicted box, the mask of the same class is attached to the instance by adding a
    new "pred_masks" field to pred_instances.

    Args:
        pred_mask_logits (Tensor): A tensor of shape (B, C, Hmask, Wmask) or (B, 1, Hmask, Wmask)
            for class-specific or class-agnostic, where B is the total number of predicted masks
            in all images, C is the number of foreground classes, and Hmask, Wmask are the height
            and width of the mask predictions. The values are logits.
        pred_instances (list[Instances]): A list of N Instances, where N is the number of images
            in the batch. Each Instances must have field "pred_classes".

    Returns:
        None. pred_instances will contain an extra "pred_masks" field storing a mask of size (Hmask,
            Wmask) for predicted class. Note that the masks are returned as a soft (non-quantized)
            masks the resolution predicted by the network; post-processing steps, such as resizing
            the predicted masks to the original image resolution and/or binarizing them, is left
            to the caller.
    """
    cls_agnostic_mask = pred_mask_logits.size(1) == 1

    if cls_agnostic_mask:
        mask_probs_pred = pred_mask_logits.sigmoid()
    else:
        # Select masks corresponding to the predicted classes
        num_masks = pred_mask_logits.shape[0]
        class_pred = cat([i.pred_classes for i in pred_instances])
        indices = torch.arange(num_masks, device=class_pred.device)
        mask_probs_pred = pred_mask_logits[indices, class_pred][:, None].sigmoid()
    # mask_probs_pred.shape: (B, 1, Hmask, Wmask)

    num_boxes_per_image = [len(i) for i in pred_instances]
    mask_probs_pred = mask_probs_pred.split(num_boxes_per_image, dim=0)

    for prob, instances in zip(mask_probs_pred, pred_instances):
        instances.pred_masks = prob  # (1, Hmask, Wmask)


class BaseMaskRCNNHead(nn.Module):
    """
    Implement the basic Mask R-CNN losses and inference logic.
    """

    def __init__(self, cfg, input_shape):
        super().__init__()
        self.vis_period = cfg.VIS_PERIOD

    def forward(self, x: Dict[str, torch.Tensor], instances: List[Instances]):
        """
        Args:
            x (dict[str,Tensor]): input region feature(s) provided by :class:`ROIHeads`.
            instances (list[Instances]): contains the boxes & labels corresponding
                to the input features.
                Exact format is up to its caller to decide.
                Typically, this is the foreground instances in training, with
                "proposal_boxes" field and other gt annotations.
                In inference, it contains boxes that are already predicted.

        Returns:
            A dict of losses in training. The predicted "instances" in inference.
        """
        x = self.layers(x)
        if self.training:
            return {"loss_mask": mask_rcnn_loss(x, instances, self.vis_period)}
        else:
            mask_rcnn_inference(x, instances)
            return instances

    def layers(self, x):
        """
        Neural network layers that makes predictions from input features.
        """
        raise NotImplementedError


@ROI_MASK_HEAD_REGISTRY.register()
class MaskRCNNConvUpsampleHead(BaseMaskRCNNHead):
    """
    A mask head with several conv layers, plus an upsample layer (with `ConvTranspose2d`).
    """

    def __init__(self, cfg, input_shape: ShapeSpec):
        """
        The following attributes are parsed from config:
            num_conv: the number of conv layers
            conv_dim: the dimension of the conv layers
            norm: normalization for the conv layers
        """
        super().__init__(cfg, input_shape)

        # fmt: off
        num_classes       = cfg.MODEL.ROI_HEADS.NUM_CLASSES
        conv_dims         = cfg.MODEL.ROI_MASK_HEAD.CONV_DIM
        self.norm         = cfg.MODEL.ROI_MASK_HEAD.NORM
        num_conv          = cfg.MODEL.ROI_MASK_HEAD.NUM_CONV
        input_channels    = input_shape.channels
        cls_agnostic_mask = cfg.MODEL.ROI_MASK_HEAD.CLS_AGNOSTIC_MASK
        # fmt: on

        self.conv_norm_relus = []

        for k in range(num_conv):
            conv = Conv2d(
                input_channels if k == 0 else conv_dims,
                conv_dims,
                kernel_size=3,
                stride=1,
                padding=1,
                bias=not self.norm,
                norm=get_norm(self.norm, conv_dims),
                activation=F.relu,
            )
            self.add_module("mask_fcn{}".format(k + 1), conv)
            self.conv_norm_relus.append(conv)

        self.deconv = ConvTranspose2d(
            conv_dims if num_conv > 0 else input_channels,
            conv_dims,
            kernel_size=2,
            stride=2,
            padding=0,
        )

        num_mask_classes = 1 if cls_agnostic_mask else num_classes
        self.predictor = Conv2d(conv_dims, num_mask_classes, kernel_size=1, stride=1, padding=0)

        for layer in self.conv_norm_relus + [self.deconv]:
            weight_init.c2_msra_fill(layer)
        # use normal distribution initialization for mask prediction layer
        nn.init.normal_(self.predictor.weight, std=0.001)
        if self.predictor.bias is not None:
            nn.init.constant_(self.predictor.bias, 0)

    def layers(self, x):
        for layer in self.conv_norm_relus:
            x = layer(x)
        x = F.relu(self.deconv(x))
        return self.predictor(x)

def bb_intersection_over_union(boxA, boxB):
    # determine the (x, y)-coordinates of the intersection rectangle
    xA = max(boxA[0], boxB[0])
    yA = max(boxA[1], boxB[1])
    xB = min(boxA[2], boxB[2])
    yB = min(boxA[3], boxB[3])

    # compute the area of intersection rectangle
    interArea = abs(max((xB - xA, 0)) * max((yB - yA), 0))
    if interArea == 0:
        return 0
    # compute the area of both the prediction and ground-truth
    # rectangles
    boxAArea = abs((boxA[2] - boxA[0]) * (boxA[3] - boxA[1]))
    boxBArea = abs((boxB[2] - boxB[0]) * (boxB[3] - boxB[1]))

    # compute the intersection over union by taking the intersection
    # area and dividing it by the sum of prediction + ground-truth
    # areas - the interesection area
    iou = interArea / float(boxAArea + boxBArea - interArea)

    # return the intersection over union value
    return iou

def build_mask_head(cfg, input_shape):
    """
    Build a mask head defined by `cfg.MODEL.ROI_MASK_HEAD.NAME`.
    """
    name = cfg.MODEL.ROI_MASK_HEAD.NAME
    return ROI_MASK_HEAD_REGISTRY.get(name)(cfg, input_shape)


def quad(c):
    c = -2 * c
    x1 = (1 + torch.sqrt(1-4*c)) / 2
    x2 = (1 - torch.sqrt(1-4*c)) / 2

    return torch.max(x1, x2)
