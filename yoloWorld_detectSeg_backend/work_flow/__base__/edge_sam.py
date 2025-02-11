import cv2
import numpy as np
import onnxruntime as ort

from typing import Tuple
from copy import deepcopy


class EdgeSAMONNX:
    def __init__(
        self, encoder_model_path, decoder_model_path, target_length
    ) -> None:
        # Load flows
        providers = ort.get_available_providers()

        # Pop TensorRT Runtime due to crashing issues
        # TODO: Add back when TensorRT backend is stable
        providers = [p for p in providers if p != "TensorrtExecutionProvider"]

        self.encoder_session = ort.InferenceSession(
            encoder_model_path, providers=providers
        )
        self.decoder_session = ort.InferenceSession(
            decoder_model_path, providers=providers
        )

        self.encoder_input_name = self.encoder_session.get_inputs()[0].name
        self.target_length = target_length

    def run_encoder(self, encoder_inputs):
        """Run encoder"""
        image_embeddings = self.encoder_session.run(None, encoder_inputs)
        return image_embeddings[0]

    @staticmethod
    def get_preprocess_shape(
        oldh: int, oldw: int, long_side_length: int
    ) -> Tuple[int, int]:
        """
        Compute the output size given input size and target long side length.
        """
        scale = long_side_length * 1.0 / max(oldh, oldw)
        newh, neww = oldh * scale, oldw * scale
        neww = int(neww + 0.5)
        newh = int(newh + 0.5)
        return (newh, neww)

    def transform(self, input_image: np.ndarray) -> np.ndarray:
        """image transform

        This function can convert the input image to the required input format for vit.

        Args:
            input_image (np.ndarray): input image, the image type should be RGB.

        Returns:
            np.ndarray: transformed image.
        """
        # Resize
        h, w, _ = input_image.shape
        target_size = self.get_preprocess_shape(h, w, self.target_length)
        input_image = cv2.resize(input_image, target_size[::-1])

        # HWC -> CHW
        input_image = input_image.transpose((2, 0, 1))

        # CHW -> NCHW
        input_image = np.ascontiguousarray(input_image)
        input_image = np.expand_dims(input_image, 0)

        return input_image

    def encode(self, cv_image):
        """
        Calculate embedding and metadata for a single image.
        """
        original_size = cv_image.shape[:2]
        encoder_inputs = {
            self.encoder_input_name: self.transform(cv_image),
        }

        image_embeddings = self.run_encoder(encoder_inputs)
        return {
            "image_embeddings": image_embeddings,
            "original_size": original_size,
        }

    def get_input_points(self, prompt):
        """Get input points"""
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
        points, labels = np.array(points).astype(np.float32), np.array(
            labels
        ).astype(np.float32)
        return points, labels

    @staticmethod
    def calculate_stability_score(
        masks: np.ndarray, mask_threshold: float, threshold_offset: float
    ) -> np.ndarray:
        """
        Computes the stability score for a batch of masks. The stability
        score is the IoU between the binary masks obtained by thresholding
        the predicted mask logits at high and low values.
        """
        high_threshold_mask = masks > (mask_threshold + threshold_offset)
        low_threshold_mask = masks > (mask_threshold - threshold_offset)

        intersections = np.sum(
            high_threshold_mask & low_threshold_mask,
            axis=(-1, -2),
            dtype=np.int32,
        )
        unions = np.sum(
            high_threshold_mask | low_threshold_mask,
            axis=(-1, -2),
            dtype=np.int32,
        )
        return intersections / unions

    def apply_coords(
        self, coords: np.ndarray, original_size: Tuple[int, ...]
    ) -> np.ndarray:
        """
        Expects a numpy array of length 2 in the final dimension. Requires the
        original image size in (H, W) format.
        """
        old_h, old_w = original_size
        new_h, new_w = self.get_preprocess_shape(
            original_size[0], original_size[1], self.target_length
        )
        coords = deepcopy(coords).astype(float)
        coords[..., 0] = coords[..., 0] * (new_w / old_w)
        coords[..., 1] = coords[..., 1] * (new_h / old_h)
        return coords

    def apply_boxes(self, boxes, original_size, new_size):
        boxes = self.apply_coords(
            boxes.reshape(-1, 2, 2), original_size, new_size
        )
        return boxes

    def postprocess_masks(
        self,
        mask: np.ndarray,
        input_size: Tuple[int, ...],
        original_size: Tuple[int, ...],
    ) -> np.ndarray:
        """
        Remove padding and upscale masks to the original image size.

        Arguments:
        mask (np.ndarray): mask from the mask_decoder, in 1xHxW format.
        input_size (tuple(int, int)): The size of the image input to the
            model, in (H, W) format. Used to remove padding.
        original_size (tuple(int, int)): The original size of the image
            before resizing for input to the model, in (H, W) format.

        Returns:
        (np.ndarray): Mask in 1xHxW format, where (H, W)
            is given by original_size.
        """
        img_size = self.target_length

        # Upscale masks to the intermediate size
        mask = cv2.resize(mask, (img_size, img_size))

        # Remove padding
        mask = mask[..., : input_size[0], : input_size[1]]

        # Upscale masks to the original size
        new_size = original_size[::-1]
        mask = cv2.resize(mask, new_size)

        return mask

    def run_decoder(self, image_embeddings, original_size, point_coords, point_labels):
        """Run decoder"""

        if point_coords is None or point_labels is None:
            raise ValueError(
                "Unable to segment, please input at least one box or point."
            )

        if point_coords is not None:
            if isinstance(point_coords, list):
                point_coords = np.array(point_coords, dtype=np.float32)
            if isinstance(point_labels, list):
                point_labels = np.array(point_labels, dtype=np.float32)

        if point_coords is not None:
            point_coords = self.apply_coords(
                point_coords, original_size
            ).astype(np.float32)
            point_coords = np.expand_dims(point_coords, axis=0)
            point_labels = np.expand_dims(point_labels, axis=0)

        assert point_coords.shape[0] == 1 and point_coords.shape[-1] == 2
        assert point_labels.shape[0] == 1

        input_dict = {
            "image_embeddings": image_embeddings,
            "point_coords": point_coords,
            "point_labels": point_labels,
        }
        scores, masks = self.decoder_session.run(None, input_dict)
        mask_threshold = 0.0
        stability_score_offset = 1.0
        scores = self.calculate_stability_score(
            masks[0], mask_threshold, stability_score_offset
        )
        max_score_index = np.argmax(scores)
        masks = masks[0, max_score_index]
        input_size = self.get_preprocess_shape(
            *original_size, self.target_length
        )
        masks = self.postprocess_masks(masks, input_size, original_size)
        masks = masks > 0.0
        return masks

    def predict_masks(self, embedding, prompt, transform_prompt=True):
        """
        Predict masks for a single image.
        """
        if transform_prompt:
            point_coords, point_labels = self.get_input_points(prompt)
        else:
            point_coords, point_labels = prompt
        masks = self.run_decoder(
            embedding["image_embeddings"],
            embedding["original_size"],
            point_coords,
            point_labels,
        )

        return masks
