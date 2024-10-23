from typing import Any, List, Tuple, Union

import torch
from hydra import compose
from hydra.utils import instantiate
from omegaconf import OmegaConf
import logging
import cv2
import numpy as np
import onnxruntime as ort
from numpy import ndarray


class SegmentAnything2ONNX:
    """Segmentation model using Segment Anything 2 (SAM2)"""

    def __init__(self, encoder_model_path, decoder_model_path, device) -> None:
        self.encoder = SAM2ImageEncoder(encoder_model_path, device)
        self.decoder = SAM2ImageDecoder(
            decoder_model_path, device, self.encoder.input_shape[2:]
        )

    def encode(self, cv_image: np.ndarray) -> List[np.ndarray]:
        original_size = cv_image.shape[:2]
        high_res_feats_0, high_res_feats_1, image_embed = self.encoder(
            cv_image
        )
        return {
            "high_res_feats_0": high_res_feats_0,
            "high_res_feats_1": high_res_feats_1,
            "image_embedding": image_embed,
            "original_size": original_size,
        }

    def predict_masks(self, embedding, prompt) -> List[np.ndarray]:
        points = []
        labels = []
        for mark in prompt:
            if mark["type"] == "point":
                points.append(mark["data"])
                labels.append(mark["label"])
            elif mark["type"] == "rectangle":
                points.append([mark["data"][0], mark["data"][1]])  # top left
                points.append(
                    [mark["data"][2], mark["data"][3]]
                )  # bottom right
                labels.append(2)
                labels.append(3)
        points, labels = np.array(points), np.array(labels)

        image_embedding = embedding["image_embedding"]
        high_res_feats_0 = embedding["high_res_feats_0"]
        high_res_feats_1 = embedding["high_res_feats_1"]
        original_size = embedding["original_size"]
        self.decoder.set_image_size(original_size)
        masks, _ = self.decoder(
            image_embedding,
            high_res_feats_0,
            high_res_feats_1,
            points,
            labels,
        )

        return masks

    def transform_masks(self, masks, original_size, transform_matrix):
        """Transform the masks back to the original image size."""
        output_masks = []
        for batch in range(masks.shape[0]):
            batch_masks = []
            for mask_id in range(masks.shape[1]):
                mask = masks[batch, mask_id]
                mask = cv2.warpAffine(
                    mask,
                    transform_matrix[:2],
                    (original_size[1], original_size[0]),
                    flags=cv2.INTER_LINEAR,
                )
                batch_masks.append(mask)
            output_masks.append(batch_masks)
        return np.array(output_masks)


class SAM2ImageEncoder:
    def __init__(self, path: str, device: str) -> None:
        # Initialize model
        providers = ["CPUExecutionProvider"]
        if device.lower() == "gpu":
            providers = ["CUDAExecutionProvider"]
        sess_options = ort.SessionOptions()
        sess_options.log_severity_level = 3
        self.session = ort.InferenceSession(
            path, providers=providers, sess_options=sess_options
        )

        # Get model info
        self.get_input_details()
        self.get_output_details()

    def __call__(
        self, image: np.ndarray
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        return self.encode_image(image)

    def encode_image(
        self, image: np.ndarray
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        input_tensor = self.prepare_input(image)

        outputs = self.forward_encoder(input_tensor)

        return self.process_output(outputs)

    def prepare_input(self, image: np.ndarray) -> np.ndarray:
        self.img_height, self.img_width = image.shape[:2]

        input_img = cv2.resize(image, (self.input_width, self.input_height))

        mean = np.array([0.485, 0.456, 0.406])
        std = np.array([0.229, 0.224, 0.225])
        input_img = (input_img / 255.0 - mean) / std
        input_img = input_img.transpose(2, 0, 1)
        input_tensor = input_img[np.newaxis, :, :, :].astype(np.float32)

        return input_tensor

    def forward_encoder(self, input_tensor: np.ndarray) -> List[np.ndarray]:
        outputs = self.session.run(
            self.output_names, {self.input_names[0]: input_tensor}
        )

        return outputs

    def process_output(
        self, outputs: List[np.ndarray]
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        return outputs[0], outputs[1], outputs[2]

    def get_input_details(self) -> None:
        model_inputs = self.session.get_inputs()
        self.input_names = [
            model_inputs[i].name for i in range(len(model_inputs))
        ]

        self.input_shape = model_inputs[0].shape
        self.input_height = self.input_shape[2]
        self.input_width = self.input_shape[3]

    def get_output_details(self) -> None:
        model_outputs = self.session.get_outputs()
        self.output_names = [
            model_outputs[i].name for i in range(len(model_outputs))
        ]


class SAM2ImageDecoder:
    def __init__(
        self,
        path: str,
        device: str,
        encoder_input_size: Tuple[int, int],
        orig_im_size: Tuple[int, int] = None,
        mask_threshold: float = 0.0,
    ) -> None:
        # Initialize model
        providers = ["CPUExecutionProvider"]
        if device.lower() == "gpu":
            providers = ["CUDAExecutionProvider"]
        sess_options = ort.SessionOptions()
        sess_options.log_severity_level = 3
        self.session = ort.InferenceSession(
            path, providers=providers, sess_options=sess_options
        )

        self.orig_im_size = (
            orig_im_size if orig_im_size is not None else encoder_input_size
        )
        self.encoder_input_size = encoder_input_size
        self.mask_threshold = mask_threshold
        self.scale_factor = 4

        # Get model info
        self.get_input_details()
        self.get_output_details()

    def __call__(
        self,
        image_embed: np.ndarray,
        high_res_feats_0: np.ndarray,
        high_res_feats_1: np.ndarray,
        point_coords: Union[List[np.ndarray], np.ndarray],
        point_labels: Union[List[np.ndarray], np.ndarray],
    ) -> Tuple[List[np.ndarray], ndarray]:
        return self.predict(
            image_embed,
            high_res_feats_0,
            high_res_feats_1,
            point_coords,
            point_labels,
        )

    def predict(
        self,
        image_embed: np.ndarray,
        high_res_feats_0: np.ndarray,
        high_res_feats_1: np.ndarray,
        point_coords: Union[List[np.ndarray], np.ndarray],
        point_labels: Union[List[np.ndarray], np.ndarray],
    ) -> Tuple[List[np.ndarray], ndarray]:
        inputs = self.prepare_inputs(
            image_embed,
            high_res_feats_0,
            high_res_feats_1,
            point_coords,
            point_labels,
        )

        outputs = self.forward_decoder(inputs)

        return self.process_output(outputs)

    def prepare_inputs(
        self,
        image_embed: np.ndarray,
        high_res_feats_0: np.ndarray,
        high_res_feats_1: np.ndarray,
        point_coords: Union[List[np.ndarray], np.ndarray],
        point_labels: Union[List[np.ndarray], np.ndarray],
    ):
        input_point_coords, input_point_labels = self.prepare_points(
            point_coords, point_labels
        )

        num_labels = input_point_labels.shape[0]
        mask_input = np.zeros(
            (
                num_labels,
                1,
                self.encoder_input_size[0] // self.scale_factor,
                self.encoder_input_size[1] // self.scale_factor,
            ),
            dtype=np.float32,
        )
        has_mask_input = np.array([0], dtype=np.float32)

        return (
            image_embed,
            high_res_feats_0,
            high_res_feats_1,
            input_point_coords,
            input_point_labels,
            mask_input,
            has_mask_input,
        )

    def prepare_points(
        self,
        point_coords: Union[List[np.ndarray], np.ndarray],
        point_labels: Union[List[np.ndarray], np.ndarray],
    ) -> Tuple[np.ndarray, np.ndarray]:
        if isinstance(point_coords, np.ndarray):
            input_point_coords = point_coords[np.newaxis, ...]
            input_point_labels = point_labels[np.newaxis, ...]
        else:
            max_num_points = max([coords.shape[0] for coords in point_coords])
            # We need to make sure that all inputs have the same number of points
            # Add invalid points to pad the input (0, 0) with -1 value for labels
            input_point_coords = np.zeros(
                (len(point_coords), max_num_points, 2), dtype=np.float32
            )
            input_point_labels = (
                np.ones((len(point_coords), max_num_points), dtype=np.float32)
                * -1
            )

            for i, (coords, labels) in enumerate(
                zip(point_coords, point_labels)
            ):
                input_point_coords[i, : coords.shape[0], :] = coords
                input_point_labels[i, : labels.shape[0]] = labels

        input_point_coords[..., 0] = (
            input_point_coords[..., 0]
            / self.orig_im_size[1]
            * self.encoder_input_size[1]
        )  # Normalize x
        input_point_coords[..., 1] = (
            input_point_coords[..., 1]
            / self.orig_im_size[0]
            * self.encoder_input_size[0]
        )  # Normalize y

        return input_point_coords.astype(
            np.float32
        ), input_point_labels.astype(np.float32)

    def forward_decoder(self, inputs) -> List[np.ndarray]:
        outputs = self.session.run(
            self.output_names,
            {
                self.input_names[i]: inputs[i]
                for i in range(len(self.input_names))
            },
        )
        return outputs

    def process_output(
        self, outputs: List[np.ndarray]
    ) -> Tuple[List[Union[np.ndarray, Any]], np.ndarray]:
        scores = outputs[1].squeeze()
        masks = outputs[0][0]

        # Select the best masks based on the scores
        best_mask = masks[np.argmax(scores)]
        best_mask = cv2.resize(
            best_mask, (self.orig_im_size[1], self.orig_im_size[0])
        )
        return (
            np.array([[best_mask]]),
            scores,
        )

    def set_image_size(self, orig_im_size: Tuple[int, int]) -> None:
        self.orig_im_size = orig_im_size

    def get_input_details(self) -> None:
        model_inputs = self.session.get_inputs()
        self.input_names = [
            model_inputs[i].name for i in range(len(model_inputs))
        ]

    def get_output_details(self) -> None:
        model_outputs = self.session.get_outputs()
        self.output_names = [
            model_outputs[i].name for i in range(len(model_outputs))
        ]

def build_sam2(
    config_file,
    ckpt_path=None,
    device="cuda",
    mode="eval",
    hydra_overrides_extra=[],
    apply_postprocessing=True,
):

    if apply_postprocessing:
        hydra_overrides_extra = hydra_overrides_extra.copy()
        hydra_overrides_extra += [
            # dynamically fall back to multi-mask if the single mask is not stable
            "++model.sam_mask_decoder_extra_args.dynamic_multimask_via_stability=true",
            "++model.sam_mask_decoder_extra_args.dynamic_multimask_stability_delta=0.05",
            "++model.sam_mask_decoder_extra_args.dynamic_multimask_stability_thresh=0.98",
        ]
    # Read config and init model
    cfg = compose(config_name=config_file, overrides=hydra_overrides_extra)
    OmegaConf.resolve(cfg)
    model = instantiate(cfg.model, _recursive_=True)
    _load_checkpoint(model, ckpt_path)
    model = model.to(device)
    if mode == "eval":
        model.eval()
    return model

def build_sam2_camera_predictor(
    config_file,
    ckpt_path=None,
    device="cuda",
    mode="eval",
    hydra_overrides_extra=[],
    apply_postprocessing=True,
):
    hydra_overrides = [
        "++model._target_=sam2.sam2_camera_predictor.SAM2CameraPredictor",
    ]
    if apply_postprocessing:
        hydra_overrides_extra = hydra_overrides_extra.copy()
        hydra_overrides_extra += [
            # dynamically fall back to multi-mask if the single mask is not stable
            "++model.sam_mask_decoder_extra_args.dynamic_multimask_via_stability=true",
            "++model.sam_mask_decoder_extra_args.dynamic_multimask_stability_delta=0.05",
            "++model.sam_mask_decoder_extra_args.dynamic_multimask_stability_thresh=0.98",
            # the sigmoid mask logits on interacted frames with clicks in the memory encoder so that the encoded masks are exactly as what users see from clicking
            "++model.binarize_mask_from_pts_for_mem_enc=true",
            # fill small holes in the low-res masks up to `fill_hole_area` (before resizing them to the original video resolution)
            "++model.fill_hole_area=8",
        ]
    hydra_overrides.extend(hydra_overrides_extra)

    # Read config and init model
    cfg = compose(config_name=config_file, overrides=hydra_overrides)
    OmegaConf.resolve(cfg)
    model = instantiate(cfg.model, _recursive_=True)
    _load_checkpoint(model, ckpt_path)
    model = model.to(device)
    if mode == "eval":
        model.eval()
    return model

def _load_checkpoint(model, ckpt_path):
    if ckpt_path is not None:
        sd = torch.load(ckpt_path, map_location="cpu")["model"]
        missing_keys, unexpected_keys = model.load_state_dict(sd)
        if missing_keys:
            logging.error(missing_keys)
            raise RuntimeError()
        if unexpected_keys:
            logging.error(unexpected_keys)
            raise RuntimeError()
        logging.info("Loaded checkpoint sucessfully")