import torch
import torch.nn as nn
import numpy as np
import math
from copy import deepcopy


def nms(dets, scores, nms_thresh=0.4):
    """"Pure Python NMS baseline."""
    x1 = dets[:, 0]  #xmin
    y1 = dets[:, 1]  #ymin
    x2 = dets[:, 2]  #xmax
    y2 = dets[:, 3]  #ymax

    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]

    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])

        w = np.maximum(1e-28, xx2 - xx1)
        h = np.maximum(1e-28, yy2 - yy1)
        inter = w * h

        # Cross Area / (bbox + particular area - Cross Area)
        ovr = inter / (areas[i] + areas[order[1:]] - inter + 1e-10)
        #reserve all the boundingbox whose ovr less than thresh
        inds = np.where(ovr <= nms_thresh)[0]
        order = order[inds + 1]

    return keep


def is_parallel(model):
    # Returns True if model is of type DP or DDP
    return type(model) in (nn.parallel.DataParallel, nn.parallel.DistributedDataParallel)


def detection_collate(batch):
    """Custom collate fn for dealing with batches of images that have a different
    number of associated object annotations (bounding boxes).

    Arguments:
        batch: (tuple) A tuple of tensor images and lists of annotations

    Return:
        A tuple containing:
            1) (tensor) batch of images stacked on their 0 dim
            2) (list of tensors) annotations for a given image are stacked on
                                 0 dim
            3) (tensor) batch of masks stacked on their 0 dim
    """
    targets = []
    images = []
    masks = []
    max_line = 0
    # obtain the max height and max width
    for sample in batch:
        h, w = sample[0].shape[-2:]
        max_line = max(max_line, max(h, w))

    for sample in batch:
        image = sample[0]
        target = sample[1]
        # zero padding
        c, h, w = image.shape[:3]
        image_with_padding = torch.zeros([c, max_line, max_line])
        image_with_padding[:, :h, :w] = image
        # image mask
        mask = torch.zeros([max_line, max_line])
        mask[:h, :w] = 1.0
        # rescale bbox
        boxes_ = target["boxes"].clone()
        boxes_[:, [0, 2]] = boxes_[:, [0, 2]] * w / max_line
        boxes_[:, [1, 3]] = boxes_[:, [1, 3]] * h / max_line
        target["boxes"] = boxes_

        images.append(image_with_padding)
        targets.append(target)
        masks.append(mask)

    images = torch.stack(images, 0) # [B, C, H, W]
    masks = torch.stack(masks, 0)   # [B, H, W]

    return images, targets, masks


# test time augmentation(TTA)
class TestTimeAugmentation(object):
    def __init__(self, num_classes=80, nms_thresh=0.4, scale_range=[320, 640, 32]):
        self.nms = nms
        self.num_classes = num_classes
        self.nms_thresh = nms_thresh
        self.scales = np.arange(scale_range[0], scale_range[1]+1, scale_range[2])
        
    def __call__(self, x, model):
        # x: Tensor -> [B, C, H, W]
        bboxes_list = []
        scores_list = []
        labels_list = []

        # multi scale
        for s in self.scales:
            if x.size(-1) == s and x.size(-2) == s:
                x_scale = x
            else:
                x_scale =torch.nn.functional.interpolate(
                                        input=x, 
                                        size=(s, s), 
                                        mode='bilinear', 
                                        align_corners=False)
            model.set_grid(s)
            bboxes, scores, labels = model(x_scale)
            bboxes_list.append(bboxes)
            scores_list.append(scores)
            labels_list.append(labels)

            # Flip
            x_flip = torch.flip(x_scale, [-1])
            bboxes, scores, labels = model(x_flip)
            bboxes = bboxes.copy()
            bboxes[:, 0::2] = 1.0 - bboxes[:, 2::-2]
            bboxes_list.append(bboxes)
            scores_list.append(scores)
            labels_list.append(labels)

        bboxes = np.concatenate(bboxes_list)
        scores = np.concatenate(scores_list)
        labels = np.concatenate(labels_list)

        # nms
        keep = np.zeros(len(bboxes), dtype=np.int)
        for i in range(self.num_classes):
            inds = np.where(labels == i)[0]
            if len(inds) == 0:
                continue
            c_bboxes = bboxes[inds]
            c_scores = scores[inds]
            c_keep = self.nms(c_bboxes, c_scores, self.nms_thresh)
            keep[inds[c_keep]] = 1

        keep = np.where(keep > 0)
        bboxes = bboxes[keep]
        scores = scores[keep]
        labels = labels[keep]

        return bboxes, scores, labels
