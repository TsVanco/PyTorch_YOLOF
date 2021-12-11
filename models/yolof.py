import numpy as np
import torch
import torch.nn as nn
from .conv import Conv
from .resnet import build_backbone
from .dilated_encoder import DilatedEncoder


class YOLOF(nn.Module):
    def __init__(self, 
                 cfg,
                 device, 
                 num_classes=20, 
                 conf_thresh = 0.05,
                 nms_thresh = 0.6,
                 trainable=False, 
                 norm='BN',
                 post_process=False):
        super(YOLOF, self).__init__()
        self.device = device
        self.fmp_size = None
        self.num_classes = num_classes
        self.trainable = trainable
        self.conf_thresh = conf_thresh
        self.nms_thresh = nms_thresh
        self.anchor_size = torch.as_tensor(cfg['anchor_size'])
        self.num_anchors = len(cfg['anchor_size'])
        self.post_process = post_process

        # backbone
        self.backbone, feature_channels, self.stride = build_backbone(
                                                            model_name=cfg['backbone'],
                                                            pretrained=trainable,
                                                            train_backbone=True,
                                                            return_interm_layers=False)

        # neck
        self.neck = DilatedEncoder(c1=feature_channels, 
                                   c2=cfg['head_dims'], 
                                   e=cfg['bottle_ratio'],
                                   dilation_list=cfg['dilated_block'])

        # head
        self.cls_feat = nn.Sequential(
            Conv(cfg['head_dims'], cfg['head_dims'], k=3, p=1, s=1, norm=norm),
            Conv(cfg['head_dims'], cfg['head_dims'], k=3, p=1, s=1, norm=norm)
        )
        self.reg_feat = nn.Sequential(
            Conv(cfg['head_dims'], cfg['head_dims'], k=3, p=1, s=1, norm=norm),
            Conv(cfg['head_dims'], cfg['head_dims'], k=3, p=1, s=1, norm=norm),
            Conv(cfg['head_dims'], cfg['head_dims'], k=3, p=1, s=1, norm=norm),
            Conv(cfg['head_dims'], cfg['head_dims'], k=3, p=1, s=1, norm=norm)
        )

        # head
        self.obj_pred = nn.Conv2d(cfg['head_dims'], self.num_anchors * 1, kernel_size=1)
        self.cls_pred = nn.Conv2d(cfg['head_dims'], self.num_anchors * self.num_classes, kernel_size=1)
        self.reg_pred = nn.Conv2d(cfg['head_dims'], self.num_anchors * 4, kernel_size=1)

        if self.trainable:
            # init bias
            self._init_head()


    def _init_head(self):  
        # init weight of decoder
        for m in [self.cls_feat, self.reg_feat]:
            if isinstance(m, nn.Conv2d):
                nn.init.normal_(m.weight, mean=0, std=0.01)
                if hasattr(m, 'bias') and m.bias is not None:
                    nn.init.constant_(m.bias, 0)

            if isinstance(m, (nn.GroupNorm, nn.BatchNorm2d, nn.SyncBatchNorm)):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)
                
        # init bias of cls_head
        init_prob = 0.01
        bias_value = -torch.log(torch.tensor((1. - init_prob) / init_prob))
        nn.init.constant_(self.cls_pred.bias, bias_value)


    def generate_anchors(self, fmp_size):
        """fmp_size: list -> [H, W] \n
           stride: int -> output stride
        """
        # check anchor boxes
        if self.fmp_size is not None and self.fmp_size == fmp_size:
            return self.anchor_boxes
        else:
            # generate grid cells
            fmp_h, fmp_w = fmp_size
            grid_y, grid_x = torch.meshgrid([torch.arange(fmp_h), torch.arange(fmp_w)])
            # [H, W, 2] -> [HW, 2]
            grid_xy = torch.stack([grid_x, grid_y], dim=-1).float().view(-1, 2) + 0.5
            # [HW, 2] -> [1, HW, 1, 2] -> [1, HW, KA, 2] 
            grid_xy = grid_xy[None, :, None, :].repeat(1, 1, self.num_anchors, 1).to(self.device)
            # [KA, 2] -> [1, 1, KA, 2] -> [1, HW, KA, 2]
            anchor_wh = self.anchor_size[None, None, :, :].repeat(1, fmp_h*fmp_w, 1, 1).to(self.device)
            anchor_wh = anchor_wh / self.stride

            # [1, HW, KA, 4]
            anchor_boxes = torch.cat([grid_xy, anchor_wh], dim=-1)

            self.anchor_boxes = anchor_boxes
            self.fmp_size = fmp_size

            return anchor_boxes
        

    def nms(self, dets, scores):
        """"Pure Python NMS."""
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
            # compute iou
            xx1 = np.maximum(x1[i], x1[order[1:]])
            yy1 = np.maximum(y1[i], y1[order[1:]])
            xx2 = np.minimum(x2[i], x2[order[1:]])
            yy2 = np.minimum(y2[i], y2[order[1:]])

            w = np.maximum(1e-28, xx2 - xx1)
            h = np.maximum(1e-28, yy2 - yy1)
            inter = w * h

            ovr = inter / (areas[i] + areas[order[1:]] - inter + 1e-14)
            #reserve all the boundingbox whose ovr less than thresh
            inds = np.where(ovr <= self.nms_thresh)[0]
            order = order[inds + 1]

        return keep


    def postprocess(self, bboxes, scores):
        """
        bboxes: (N, 4), bsize = 1
        scores: (N, C), bsize = 1
        """

        cls_inds = np.argmax(scores, axis=1)
        scores = scores[(np.arange(scores.shape[0]), cls_inds)]
        
        # threshold
        keep = np.where(scores >= self.conf_thresh)
        bboxes = bboxes[keep]
        scores = scores[keep]
        cls_inds = cls_inds[keep]

        # NMS
        keep = np.zeros(len(bboxes), dtype=np.int)
        for i in range(self.num_classes):
            inds = np.where(cls_inds == i)[0]
            if len(inds) == 0:
                continue
            c_bboxes = bboxes[inds]
            c_scores = scores[inds]
            c_keep = self.nms(c_bboxes, c_scores)
            keep[inds[c_keep]] = 1

        keep = np.where(keep > 0)
        bboxes = bboxes[keep]
        scores = scores[keep]
        cls_inds = cls_inds[keep]

        return bboxes, scores, cls_inds


    def forward(self, x, mask=None):
        img_h, img_w = x.shape[2:]
        # backbone
        x = self.backbone(x)[-1]

        # neck
        x = self.neck(x)
        B, _, H, W = x.size()

        # head
        cls_feat = self.cls_feat(x)
        reg_feat = self.reg_feat(x)

        # pred
        obj_pred = self.obj_pred(reg_feat)
        cls_pred = self.cls_pred(cls_feat)
        reg_pred = self.reg_pred(reg_feat)

        # implicit objectness
        obj_pred = obj_pred.view(B, -1, 1, H, W)
        cls_pred = cls_pred.view(B, -1, self.num_classes, H, W)
        normalized_cls_pred = cls_pred + obj_pred - torch.log(
            1. + torch.clamp(cls_pred.exp(), max=1e4) + torch.clamp(
                obj_pred.exp(), max=1e4))
        # [B, KA, C, H, W] -> [B, H, W, KA, C] -> [B, HW, KA, C]
        normalized_cls_pred = normalized_cls_pred.permute(0, 3, 4, 1, 2).contiguous()
        normalized_cls_pred = normalized_cls_pred.view(B, -1, self.num_anchors, self.num_classes)

        # decode box
        anchor_boxes = self.generate_anchors(fmp_size=[H, W]) # [1, HW, KA, 4]
        # [B, KA*4, H, W] -> [B, KA, 4, H, W] -> [B, H, W, KA, 4] -> [B, HW, KA, 4]
        reg_pred =reg_pred.view(B, -1, 4, H, W).permute(0, 3, 4, 1, 2).contiguous()
        reg_pred = reg_pred.view(B, -1, self.num_anchors, 4)
        xy_pred = reg_pred[..., :2] + anchor_boxes[..., :2]
        wh_pred = reg_pred[..., 2:].exp() * anchor_boxes[..., 2:]
        # convert [x, y, w, h] -> [x1, y1, x2, y2]
        x1y1_pred = xy_pred - wh_pred / 2.0
        x2y2_pred = xy_pred + wh_pred / 2.0
        box_pred = torch.cat([x1y1_pred, x2y2_pred], dim=-1)

        if self.post_process:
            with torch.no_grad():
                # [B, HW, KA, C] -> [HW x KA, C], where B = 1.
                normalized_cls_pred = normalized_cls_pred[0].view(-1, self.num_classes)
                box_pred = box_pred[0].view(-1, 4)

                scores = normalized_cls_pred.sigmoid()

                # rescale bbox
                box_pred = box_pred * self.stride

                # normalize bbox
                box_pred[..., [0, 2]] /= img_w
                box_pred[..., [1, 3]] /= img_h
                box_pred = box_pred.clamp(0., 1.)

                # to cpu
                scores = scores.cpu().numpy()
                bboxes = box_pred.cpu().numpy()

                # post-process
                bboxes, scores, cls_inds = self.postprocess(bboxes, scores)

                return bboxes, scores, cls_inds

        else:
            if mask is not None:
                # [B, H, W]
                mask = torch.nn.functional.interpolate(mask[None], size=[H, W]).bool()[0]
                # [B, HW]
                mask = mask.flatten(1)

            outputs = {"pred_cls": normalized_cls_pred,
                        "pred_box": box_pred,
                        "mask": mask}
            return outputs 


def build_model(args, cfg, device, num_classes=80, trainable=False, post_process=False):
    yolof = YOLOF(cfg=cfg,
                  device=device,
                  num_classes=num_classes,
                  trainable=trainable,
                  norm=args.norm,
                  conf_thresh=args.conf_thresh,
                  nms_thresh=args.nms_thresh,
                  post_process=post_process)

    # SyncBatchNorm
    if args.sybn and args.cuda and args.num_gpu > 1:
        print('use SyncBatchNorm ...')
        yolof = torch.nn.SyncBatchNorm.convert_sync_batchnorm(yolof)

    return yolof
