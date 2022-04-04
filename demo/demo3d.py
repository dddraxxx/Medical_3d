# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
import argparse
from functools import reduce
import glob
import multiprocessing as mp
import os
import time
import cv2
import torch
import tqdm
from torchvision.utils import draw_segmentation_masks
from torch.utils.data import DataLoader

from detectron2.data.detection_utils import read_image
from detectron2.utils.logger import setup_logger
from adet.utils.comm import aligned_bilinear3d

from predictor3d import VisualizationDemo
from adet.config import get_cfg
from adet.utils.volume_utils import read_niigz
from detectron2.config import CfgNode

from adet.utils.dataset_3d import Volumes, get_dataset, read_volume, save_volume
from adet.utils.visualize_niigz import (
    PyTMinMaxScalerVectorized,
    visulize_3d,
    draw_3d_box_on_vol,
)

# constants
WINDOW_NAME = "COCO detections"


def setup_cfg_3d(args):
    cfg = CfgNode()
    cfg.merge_from_file(args.config_file)
    cfg.merge_from_list(args.opts)
    cfg.freeze()
    return cfg


def setup_cfg(args):
    # load config from file and command-line arguments
    cfg = get_cfg()
    cfg.set_new_allowed(True)
    cfg.merge_from_file(args.config_file)
    cfg.merge_from_list(args.opts)
    # Set score_threshold for builtin models
    cfg.MODEL.RETINANET.SCORE_THRESH_TEST = args.confidence_threshold
    cfg.MODEL.ROI_HEADS.SCORE_THRESH_TEST = args.confidence_threshold
    # cfg.MODEL.FCOS.INFERENCE_TH_TEST = args.confidence_threshold
    cfg.MODEL.MEInst.INFERENCE_TH_TEST = args.confidence_threshold
    cfg.MODEL.PANOPTIC_FPN.COMBINE.INSTANCES_CONFIDENCE_THRESH = (
        args.confidence_threshold
    )
    cfg.freeze()
    return cfg


def get_parser():
    parser = argparse.ArgumentParser(description="Detectron2 Demo")
    parser.add_argument(
        "--config-file",
        default="configs/quick_schedules/e2e_mask_rcnn_R_50_FPN_inference_acc_test.yaml",
        metavar="FILE",
        help="path to config file",
    )
    parser.add_argument(
        "--webcam", action="store_true", help="Take inputs from webcam."
    )
    parser.add_argument("--video-input", help="Path to video file.")
    parser.add_argument(
        "--input", nargs="+", help="A list of space separated input images"
    )
    parser.add_argument(
        "-o",
        "--output",
        help="A file or directory to save output visualizations. "
        "If not given, will show output in an OpenCV window.",
    )

    parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=0.3,
        help="Minimum score for instance predictions to be shown",
    )
    parser.add_argument(
        "--opts",
        help="Modify config options using the command-line 'KEY VALUE' pairs",
        default=[],
        nargs=argparse.REMAINDER,
    )
    return parser


def pred_batch(batch, model):
    imgs = [i["image"] for i in batch]
    gt = [i["instances"].gt_masks.cpu() for i in batch]
    idx = [i["index"] for i in batch]
    input = []
    for i in imgs:
        depth, height, width = i.shape[-3:]
        input.append(dict(image=i, depth=depth, height=height, width=width))
    with torch.no_grad():
        results = model(input)
    results = [r["instances"].to("cpu") for r in results]
    return results, gt, idx


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    args = get_parser().parse_args()
    logger = setup_logger()
    logger.info("Arguments: " + str(args))

    cfg = setup_cfg(args)

    demo = VisualizationDemo(cfg)

    if args.input:
        if os.path.isdir(args.input[0]):
            args.input = [
                os.path.join(args.input[0], fname)
                for fname in os.listdir(args.input[0])
            ]
        elif len(args.input) == 1:
            args.input = glob.glob(os.path.expanduser(args.input[0]))
            assert args.input, "The input path(s) was not found"
        for path in tqdm.tqdm(args.input, disable=not args.output):

            # now pred cases 240-300
            # from detectron2.data.build import trivial_batch_collator
            # pred_ds = get_dataset(60)
            # pred_ds.data = list(range(240,300))
            # pred_ds.data.remove(296)
            # pred_dl = DataLoader(pred_ds, 10, collate_fn= trivial_batch_collator, shuffle=False, pin_memory=True,   num_workers=12, drop_last=False)
            # res = []
            # for batch in iter(pred_dl):
            #     print([i['index'] for i in batch])
            #     re = pred_batch(batch, demo.predictor.model)
            #     re = list(zip(*re))
            #     print(len(re))
            #     res.extend(re)
            # from pathlib import Path as pa
            # dest = pa('pred_fu1')
            # dest.mkdir(exist_ok=True)
            # for r in res:
            #     d = dest / '{:05d}.npy'.format(r[-1])
            #     print(f'save to {d}')
            #     torch.save(r, d)
            # print('finished')
            # break

            # use PIL, to be consistent with evaluation
            # img = read_image(path, format="BGR")
            # modified
            ds = Volumes(1)
            normalizer = lambda x: (x - x.mean(dim=[1, 2, 3], keepdim=True)) / x.std(
                dim=[1, 2, 3], keepdim=True
            )
            # ds[path]
            img, lab, gt = ds.get_data(1, read_gt=True)
            header = ds.header
            # with open('gt_boxes.txt', 'w') as fout:
            #     fout.write(str(lab))

            # normalize
            print("Input shape: {}".format(img.shape))
            # st = torch.tensor([76, 212, 226])
            # end = st+128
            # img = img[:, st[0]:end[0],st[1]:end[1],st[2]:end[2]]

            # save_volume('input1.nii.gz', img[0], header)
            img = normalizer(img)
            img_n = PyTMinMaxScalerVectorized()(img, dim=3)
            # PyTMinMaxScalerVectorized()(img.float())[0]
            visulize_3d(
                draw_3d_box_on_vol(img_n, lab), 5, 5, save_name="0inst_data_3d_all.png"
            )
            print("label is: {}".format(lab))
            img = img.numpy()

            start_time = time.time()
            demo.predictor._3d = True
            predictions = demo.run_on_image(img)
            predictions = predictions[0]

            im_inds = predictions["instances"].im_inds.tolist()
            print("img_ids for predicted boxes: {}".format(im_inds))
            pred_msks = predictions["instances"].pred_masks.to("cpu")
            print(pred_msks.shape, (pred_msks == 1).sum(), (pred_msks == 0).sum())
            print("Output shape: {}".format(pred_msks.shape))
            dst = 2
            tmp = pred_msks.amax(dim=[0, 2, 3])
            low, high = tmp.nonzero()[[0, -1]].squeeze().tolist()

            lab[0, 0] = max(lab[0, 0] - low + 1, 0) // dst
            lab[0, 3] = (min(high, lab[0, 3]) + 1 - low) // dst
            visulize_3d(
                draw_3d_box_on_vol(img_n[:, low : high + 1 : dst], lab),
                inter_dst=1,
                save_name="0inst_data_3d_0.png",
            )
            visulize_3d(
                draw_3d_box_on_vol(gt[:, low : high + 1 : dst], lab),
                inter_dst=1,
                save_name="0inst_gt_3d_0.png",
            )

            print(low, high, lab)
            d = img_n[0][low : high + 1 : dst]
            for i, p in enumerate([pred_msks.cpu()[:1]]):
                p = p[:, low : high + 1 : dst].transpose(0, 1)
                res = []
                for d1, p1 in zip(d, p):
                    res.append(
                        draw_segmentation_masks(
                            (d1 * 255).repeat(3, 1, 1).to(torch.uint8),
                            p1.bool(),
                            alpha=0.6,
                            colors=["red", "green", "pink"],
                        )
                    )
                visulize_3d(
                    torch.stack(res) / 255,
                    inter_dst=1,
                    save_name="0inst_pred_3d_{}.png".format(i),
                )

            logger.info(
                "{}: detected {} instances in {:.2f}s".format(
                    path, len(predictions), time.time() - start_time
                )
            )

            # if args.output:
            #     if os.path.isdir(args.output):
            #         assert os.path.isdir(args.output), args.output
            #         out_filename = os.path.join(args.output, os.path.basename(path))
            #     else:
            #         assert len(args.input) == 1, "Please specify a directory with args.output"
            #         out_filename = args.output
            #     visualized_output.save(out_filename)
            # else:
            #     cv2.imshow(WINDOW_NAME, visualized_output.get_image()[:, :, ::-1])
            #     if cv2.waitKey(0) == 27:
            #         break  # esc to quit
    elif args.webcam:
        assert args.input is None, "Cannot have both --input and --webcam!"
        cam = cv2.VideoCapture(0)
        for vis in tqdm.tqdm(demo.run_on_video(cam)):
            cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
            cv2.imshow(WINDOW_NAME, vis)
            if cv2.waitKey(1) == 27:
                break  # esc to quit
        cv2.destroyAllWindows()
    elif args.video_input:
        video = cv2.VideoCapture(args.video_input)
        width = int(video.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(video.get(cv2.CAP_PROP_FRAME_HEIGHT))
        frames_per_second = video.get(cv2.CAP_PROP_FPS)
        num_frames = int(video.get(cv2.CAP_PROP_FRAME_COUNT))
        basename = os.path.basename(args.video_input)

        if args.output:
            if os.path.isdir(args.output):
                output_fname = os.path.join(args.output, basename)
                output_fname = os.path.splitext(output_fname)[0] + ".mkv"
            else:
                output_fname = args.output
            assert not os.path.isfile(output_fname), output_fname
            output_file = cv2.VideoWriter(
                filename=output_fname,
                # some installation of opencv may not support x264 (due to its license),
                # you can try other format (e.g. MPEG)
                fourcc=cv2.VideoWriter_fourcc(*"x264"),
                fps=float(frames_per_second),
                frameSize=(width, height),
                isColor=True,
            )
        assert os.path.isfile(args.video_input)
        for vis_frame in tqdm.tqdm(demo.run_on_video(video), total=num_frames):
            if args.output:
                output_file.write(vis_frame)
            else:
                cv2.namedWindow(basename, cv2.WINDOW_NORMAL)
                cv2.imshow(basename, vis_frame)
                if cv2.waitKey(1) == 27:
                    break  # esc to quit
        video.release()
        if args.output:
            output_file.release()
        else:
            cv2.destroyAllWindows()
