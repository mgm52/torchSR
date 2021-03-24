# Implementation from https://github.com/sanghyun-son/EDSR-PyTorch
# Pretrained models from https://github.com/yulunzhang/RCAN
## ECCV-2018-Image Super-Resolution Using Very Deep Residual Channel Attention Networks
## https://arxiv.org/abs/1807.02758

import math
import torch
import torch.nn as nn
from torchvision.models.utils import load_state_dict_from_url

__all__ = [ 'rcan', 'rcan_g10r20f64', ]

url = {
    'g10r20f64x2': 'https://drive.google.com/uc?export=download&id=1aV7M0vaj0tmPHGFolXo6h23qXPPNIBgD',
    'g10r20f64x3': 'https://drive.google.com/uc?export=download&id=1NfR1eqswNYMtegt5Et0I__L_2-MT2MXB',
    'g10r20f64x4': 'https://drive.google.com/uc?export=download&id=1hMrnDTOTMOYHr-L9puMBqjqDkjeqKIbD',
    'g10r20f64x8': 'https://drive.google.com/uc?export=download&id=1ovRngDYVTaOTp69TZb6nB-hT8ihyJFJo',
}

def default_conv(in_channels, out_channels, kernel_size, bias=True):
    return nn.Conv2d(
        in_channels, out_channels, kernel_size,
        padding=(kernel_size//2), bias=bias)


class MeanShift(nn.Conv2d):
    def __init__(
        self, rgb_range,
        rgb_mean=(0.4488, 0.4371, 0.4040), rgb_std=(1.0, 1.0, 1.0), sign=-1):

        super(MeanShift, self).__init__(3, 3, kernel_size=1)
        std = torch.Tensor(rgb_std)
        self.weight.data = torch.eye(3).view(3, 3, 1, 1) / std.view(3, 1, 1, 1)
        self.bias.data = sign * rgb_range * torch.Tensor(rgb_mean) / std
        for p in self.parameters():
            p.requires_grad = False


class Upsampler(nn.Sequential):
    def __init__(self, conv, scale, n_feats, bn=False, act=False, bias=True):

        m = []
        if (scale & (scale - 1)) == 0:    # Is scale = 2^n?
            for _ in range(int(math.log(scale, 2))):
                m.append(conv(n_feats, 4 * n_feats, 3, bias))
                m.append(nn.PixelShuffle(2))
                if bn:
                    m.append(nn.BatchNorm2d(n_feats))
                if act == 'relu':
                    m.append(nn.ReLU(True))
                elif act == 'prelu':
                    m.append(nn.PReLU(n_feats))

        elif scale == 3:
            m.append(conv(n_feats, 9 * n_feats, 3, bias))
            m.append(nn.PixelShuffle(3))
            if bn:
                m.append(nn.BatchNorm2d(n_feats))
            if act == 'relu':
                m.append(nn.ReLU(True))
            elif act == 'prelu':
                m.append(nn.PReLU(n_feats))
        else:
            raise NotImplementedError

        super(Upsampler, self).__init__(*m)


## Channel Attention (CA) Layer
class CALayer(nn.Module):
    def __init__(self, channel, reduction=16):
        super(CALayer, self).__init__()
        # global average pooling: feature --> point
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        # feature channel downscale and upscale --> channel weight
        self.conv_du = nn.Sequential(
                nn.Conv2d(channel, channel // reduction, 1, padding=0, bias=True),
                nn.ReLU(inplace=True),
                nn.Conv2d(channel // reduction, channel, 1, padding=0, bias=True),
                nn.Sigmoid()
        )

    def forward(self, x):
        y = self.avg_pool(x)
        y = self.conv_du(y)
        return x * y


## Residual Channel Attention Block (RCAB)
class RCAB(nn.Module):
    def __init__(
        self, conv, n_feat, kernel_size, reduction,
        bias=True, bn=False, act=nn.ReLU(True)):

        super(RCAB, self).__init__()
        modules_body = []
        for i in range(2):
            modules_body.append(conv(n_feat, n_feat, kernel_size, bias=bias))
            if bn: modules_body.append(nn.BatchNorm2d(n_feat))
            if i == 0: modules_body.append(act)
        modules_body.append(CALayer(n_feat, reduction))
        self.body = nn.Sequential(*modules_body)

    def forward(self, x):
        res = self.body(x)
        res += x
        return res


## Residual Group (RG)
class ResidualGroup(nn.Module):
    def __init__(self, conv, n_feat, kernel_size, reduction, act, n_resblocks):
        super(ResidualGroup, self).__init__()
        modules_body = []
        modules_body = [
            RCAB(
                conv, n_feat, kernel_size, reduction, bias=True, bn=False, act=nn.ReLU(True)) \
            for _ in range(n_resblocks)]
        modules_body.append(conv(n_feat, n_feat, kernel_size))
        self.body = nn.Sequential(*modules_body)

    def forward(self, x):
        res = self.body(x)
        res += x
        return res


## Residual Channel Attention Network (RCAN)
class RCAN(nn.Module):
    def __init__(self, n_resgroups, n_resblocks, n_feats, reduction, scale, pretrained=False):
        super(RCAN, self).__init__()
        
        kernel_size = 3
        n_colors = 3
        rgb_range = 255
        conv=default_conv
        act = nn.ReLU(True)
        url_name = 'g{}r{}f{}x{}'.format(n_resgroups, n_resblocks, n_feats, scale)
        if url_name in url:
            self.url = url[url_name]
        else:
            self.url = None
        self.filename = f"rcan_{url_name}.pt"
        
        # RGB mean for DIV2K
        self.sub_mean = MeanShift(rgb_range)
        
        # define head module
        modules_head = [conv(n_colors, n_feats, kernel_size)]

        # define body module
        modules_body = [
            ResidualGroup(
                conv, n_feats, kernel_size, reduction, act=act, n_resblocks=n_resblocks) \
            for _ in range(n_resgroups)]

        modules_body.append(conv(n_feats, n_feats, kernel_size))

        # define tail module
        modules_tail = [
            Upsampler(conv, scale, n_feats, act=False),
            conv(n_feats, n_colors, kernel_size)]

        self.add_mean = MeanShift(rgb_range, sign=1)

        self.head = nn.Sequential(*modules_head)
        self.body = nn.Sequential(*modules_body)
        self.tail = nn.Sequential(*modules_tail)

        if pretrained:
            self.load_pretrained()

    def forward(self, x):
        x = self.sub_mean(255 * x)
        x = self.head(x)

        res = self.body(x)
        res += x

        x = self.tail(res)
        x = self.add_mean(x) / 255

        return x 

    def load_pretrained(self):
        if self.url is None:
            raise KeyError("No URL available for this model")
        state_dict = load_state_dict_from_url(self.url, progress=True, file_name=self.filename)
        self.load_state_dict(state_dict)


def rcan_g10r20f64(scale, pretrained=False):
    return RCAN(n_resgroups=10, n_resblocks=20, n_feats=64, reduction=16,
                scale=scale, pretrained=pretrained)


def rcan(scale, pretrained=False):
    return rcan_g10r20f64(scale, pretrained)