import os
import threading

import cv2
import traceback
import onnxruntime
import numpy as np

from copy import deepcopy

import logging

from . import __preferred_device__, Shape, AutoLabelingResult, ChineseClipONNX, LRUCache
from ..__base__.sam_hq import SegmentAnythingHQONNX
from ..engines.model import Model




class SAM_HQ(Model):
    """Segmentation model using SAM_HQ"""

    class Meta:
        required_config_names = [
            "type",
            "name",
            "display_name",
            "encoder_model_path",
            "decoder_model_path",
        ]
        widgets = [
            "output_label",
            "output_select_combobox",
            "button_add_point",
            "button_remove_point",
            "button_add_rect",
            "button_clear",
            "button_finish_object",
        ]
        output_modes = {
            "polygon": "Polygon",
            "rectangle": "Rectangle",
            "rotation": "Rotation",
        }
        default_output_mode = "polygon"

    def __init__(self, config_path, on_message) -> None:
        # Run the parent class's init method
        super().__init__(config_path, on_message)
        self.input_size = self.config["input_size"]
        self.max_width = self.config["max_width"]
        self.max_height = self.config["max_height"]

        # Get encoder and decoder model paths
        encoder_model_abs_path = self.get_model_abs_path(
            self.config, "encoder_model_path"
        )
        if not encoder_model_abs_path or not os.path.isfile(
            encoder_model_abs_path
        ):
            raise FileNotFoundError(
                "Could not download or initialize encoder of SAM_HQ."
            )
        decoder_model_abs_path = self.get_model_abs_path(
            self.config, "decoder_model_path"
        )
        if not decoder_model_abs_path or not os.path.isfile(
            decoder_model_abs_path
        ):
            raise FileNotFoundError(
                "Could not download or initialize decoder of SAM_HQ."
            )

        # Load flows
        self.model = SegmentAnythingHQONNX(
            encoder_model_abs_path, decoder_model_abs_path
        )

        # Mark for auto labeling
        # points, rectangles
        self.marks = []

        # Cache for image embedding
        self.cache_size = 10
        self.preloaded_size = self.cache_size - 3
        self.image_embedding_cache = LRUCache(self.cache_size)

        # Pre-inference worker
        self.pre_inference_thread = None
        self.pre_inference_worker = None
        self.stop_inference = False

        # CLIP flows
        self.clip_net = None
        clip_txt_model_path = self.config.get("txt_model_path", "")
        clip_img_model_path = self.config.get("img_model_path", "")
        if clip_txt_model_path and clip_img_model_path:
            if self.config["model_type"] == "cn_clip":
                clip_txt_model_path = self.get_model_abs_path(
                    self.config, "txt_model_path"
                )
                _ = self.get_model_abs_path(self.config, "txt_extra_path")
                clip_img_model_path = self.get_model_abs_path(
                    self.config, "img_model_path"
                )
                _ = self.get_model_abs_path(self.config, "img_extra_path")
                model_arch = self.config["model_arch"]
                self.clip_net = ChineseClipONNX(
                    clip_txt_model_path,
                    clip_img_model_path,
                    model_arch,
                    device=__preferred_device__,
                )
            self.classes = self.config.get("classes", [])

    def set_auto_labeling_marks(self, marks):
        """Set auto labeling marks"""
        self.marks = marks

    def post_process(self, masks, image=None):
        """
        Post process masks
        """
        # Find contours
        masks[masks > 0.0] = 255
        masks[masks <= 0.0] = 0
        masks = masks.astype(np.uint8)
        contours, _ = cv2.findContours(
            masks, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE
        )

        # Refine contours
        approx_contours = []
        for contour in contours:
            # Approximate contour
            epsilon = 0.001 * cv2.arcLength(contour, True)
            approx = cv2.approxPolyDP(contour, epsilon, True)
            approx_contours.append(approx)

        # Remove too big contours ( >90% of image size)
        if len(approx_contours) > 1:
            image_size = masks.shape[0] * masks.shape[1]
            areas = [cv2.contourArea(contour) for contour in approx_contours]
            filtered_approx_contours = [
                contour
                for contour, area in zip(approx_contours, areas)
                if area < image_size * 0.9
            ]

        # Remove small contours (area < 20% of average area)
        if len(approx_contours) > 1:
            areas = [cv2.contourArea(contour) for contour in approx_contours]
            avg_area = np.mean(areas)

            filtered_approx_contours = [
                contour
                for contour, area in zip(approx_contours, areas)
                if area > avg_area * 0.2
            ]
            approx_contours = filtered_approx_contours

        # Contours to shapes
        shapes = []
        if self.output_mode == "polygon":
            for approx in approx_contours:
                # Scale points
                points = approx.reshape(-1, 2)
                points[:, 0] = points[:, 0]
                points[:, 1] = points[:, 1]
                points = points.tolist()
                if len(points) < 3:
                    continue
                points.append(points[0])

                # Create shape
                shape = Shape(flags={})
                for point in points:
                    point[0] = int(point[0])
                    point[1] = int(point[1])
                    shape.add_point(point[0], point[1])
                shape.shape_type = "polygon"
                shape.closed = True
                shape.fill_color = "#000000"
                shape.line_color = "#000000"
                shape.line_width = 1
                shape.label = "AUTOLABEL_OBJECT"
                shape.selected = False
                shapes.append(shape)
        elif self.output_mode in ["rectangle", "rotation"]:
            x_min = 100000000
            y_min = 100000000
            x_max = 0
            y_max = 0
            for approx in approx_contours:
                # Scale points
                points = approx.reshape(-1, 2)
                points[:, 0] = points[:, 0]
                points[:, 1] = points[:, 1]
                points = points.tolist()
                if len(points) < 3:
                    continue

                # Get min/max
                for point in points:
                    x_min = min(x_min, point[0])
                    y_min = min(y_min, point[1])
                    x_max = max(x_max, point[0])
                    y_max = max(y_max, point[1])

            # Create shape
            shape = Shape(flags={})
            shape.add_point(x_min, y_min)
            shape.add_point(x_max, y_min)
            shape.add_point(x_max, y_max)
            shape.add_point(x_min, y_max)
            shape.shape_type = (
                "rectangle" if self.output_mode == "rectangle" else "rotation"
            )
            shape.closed = True
            shape.fill_color = "#000000"
            shape.line_color = "#000000"
            shape.line_width = 1
            if self.clip_net is not None and self.classes:
                img = image[y_min:y_max, x_min:x_max]
                out = self.clip_net(img, self.classes)
                shape.cache_label = self.classes[int(np.argmax(out))]
            shape.label = "AUTOLABEL_OBJECT"  # 以点为细粒度
            shape.selected = False
            shapes.append(shape)

        return shapes

    def predict_shapes(self, cv_image, filename=None) -> AutoLabelingResult:
        """
        Predict shapes from image
        """
        if cv_image is None or not self.marks:
            return AutoLabelingResult([], replace=False)

        shapes = []
        try:
            # Use cached image embedding if possible
            cached_data = self.image_embedding_cache.get(filename)
            if cached_data is not None:
                image_embedding = cached_data
            else:
                if self.stop_inference:
                    return AutoLabelingResult([], replace=False)
                image_embedding = self.model.encode(cv_image)
                self.image_embedding_cache.put(
                    filename,
                    image_embedding,
                )
            if self.stop_inference:
                return AutoLabelingResult([], replace=False)
            masks = self.model.predict_masks(image_embedding, self.marks)
            if len(masks.shape) == 4:
                masks = masks[0][0]
            else:
                masks = masks[0]
            shapes = self.post_process(masks, cv_image)
        except Exception as e:  # noqa
            logging.warning("Could not inference model")
            logging.warning(e)
            traceback.print_exc()
            return AutoLabelingResult([], replace=False)

        result = AutoLabelingResult(shapes, replace=False)
        return result

    def unload(self):
        self.stop_inference = True
        if self.pre_inference_thread:
            self.pre_inference_thread.quit()

    def preload_worker(self, files):
        """
        Preload next files, run inference and cache results
        """
        files = files[: self.preloaded_size]
        for filename in files:
            if self.image_embedding_cache.find(filename):
                continue
            cv_image = self.load_image_from_filename(filename)
            if cv_image is None:
                continue
            if self.stop_inference:
                return
            image_embedding = self.model.encode(cv_image)
            self.image_embedding_cache.put(
                filename,
                image_embedding,
            )

    def on_next_files_changed(self, next_files):
        """
        Handle next files changed. This function can preload next files
        and run inference to save time for user.
        """
        if (
            self.pre_inference_thread is None
            or not self.pre_inference_thread.isRunning()
        ):
            self.pre_inference_thread = threading.Thread(
                target=self.preload_worker, args=(next_files,)
            )
            self.pre_inference_thread.start()
