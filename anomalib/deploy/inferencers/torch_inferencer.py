"""This module contains Torch inference implementations."""

# Copyright (C) 2022 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path
from typing import Any, Dict, Optional, Union

import cv2
import numpy as np
import torch
from omegaconf import DictConfig, ListConfig
from torch import Tensor

from anomalib.config import get_configurable_parameters
from anomalib.data import TaskType
from anomalib.data.utils import InputNormalizationMethod, get_transforms
from anomalib.data.utils.boxes import masks_to_boxes
from anomalib.deploy.export import get_model_metadata
from anomalib.models import get_model
from anomalib.models.components import AnomalyModule

from .base_inferencer import Inferencer


class TorchInferencer(Inferencer):
    """PyTorch implementation for the inference.

    Args:
        config (Union[str, Path, DictConfig, ListConfig]): Configurable parameters that are used
            during the training stage.
        model_source (Union[str, Path, AnomalyModule]): Path to the model ckpt file or the Anomaly model.
        meta_data_path (Union[str, Path], optional): Path to metadata file. If none, it tries to load the params
                from the model state_dict. Defaults to None.
        device (Optional[str], optional): Device to use for inference. Options are auto, cpu, cuda. Defaults to "auto".
    """

    def __init__(
        self,
        config: Union[str, Path, DictConfig, ListConfig],
        model_source: Union[str, Path, AnomalyModule],
        meta_data_path: Optional[Union[str, Path]] = None,
        device: str = "auto",
    ):

        self.device = self._get_device(device)

        # Check and load the configuration
        if isinstance(config, (str, Path)):
            self.config = get_configurable_parameters(config_path=config)
        elif isinstance(config, (DictConfig, ListConfig)):
            self.config = config
        else:
            raise ValueError(f"Unknown config type {type(config)}")

        # Check and load the model weights.
        if isinstance(model_source, AnomalyModule):
            self.model = model_source
        else:
            self.model = self.load_model(model_source)

        self.meta_data = self._load_meta_data(meta_data_path)

    @staticmethod
    def _get_device(device: str) -> torch.device:
        """Get the device to use for inference.

        Args:
            device (str): Device to use for inference. Options are auto, cpu, cuda.

        Returns:
            torch.device: Device to use for inference.
        """
        if device not in ("auto", "cpu", "cuda", "gpu"):
            raise ValueError(f"Unknown device {device}")

        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        elif device == "gpu":
            device = "cuda"
        return torch.device(device)

    def _load_meta_data(self, path: Optional[Union[str, Path]] = None) -> Union[Dict, DictConfig]:
        """Load metadata from file or from model state dict.

        Args:
            path (Optional[Union[str, Path]], optional): Path to metadata file. If none, it tries to load the params
                from the model state_dict. Defaults to None.

        Returns:
            Dict: Dictionary containing the meta_data.
        """
        meta_data: Union[DictConfig, Dict[str, Union[float, Tensor, np.ndarray]]]
        if path is None:
            meta_data = get_model_metadata(self.model)
        else:
            meta_data = super()._load_meta_data(path)
        return meta_data

    def load_model(self, path: Union[str, Path]) -> AnomalyModule:
        """Load the PyTorch model.

        Args:
            path (Union[str, Path]): Path to model ckpt file.

        Returns:
            (AnomalyModule): PyTorch Lightning model.
        """
        model = get_model(self.config)
        model.load_state_dict(torch.load(path, map_location=self.device)["state_dict"])
        model.eval()
        return model.to(self.device)

    def pre_process(self, image: np.ndarray) -> Tensor:
        """Pre process the input image by applying transformations.

        Args:
            image (np.ndarray): Input image

        Returns:
            Tensor: pre-processed image.
        """
        transform_config = (
            self.config.dataset.transform_config.eval if "transform_config" in self.config.dataset.keys() else None
        )

        image_size = (self.config.dataset.image_size[0], self.config.dataset.image_size[1])
        center_crop = self.config.dataset.get("center_crop")
        if center_crop is not None:
            center_crop = tuple(center_crop)
        normalization = InputNormalizationMethod(self.config.dataset.normalization)
        transform = get_transforms(
            config=transform_config, image_size=image_size, center_crop=center_crop, normalization=normalization
        )
        processed_image = transform(image=image)["image"]

        if len(processed_image) == 3:
            processed_image = processed_image.unsqueeze(0)

        return processed_image.to(self.device)

    def forward(self, image: Tensor) -> Tensor:
        """Forward-Pass input tensor to the model.

        Args:
            image (Tensor): Input tensor.

        Returns:
            Tensor: Output predictions.
        """
        return self.model(image)

    def post_process(self, predictions: Tensor, meta_data: Optional[Union[Dict, DictConfig]] = None) -> Dict[str, Any]:
        """Post process the output predictions.

        Args:
            predictions (Tensor): Raw output predicted by the model.
            meta_data (Dict, optional): Meta data. Post-processing step sometimes requires
                additional meta data such as image shape. This variable comprises such info.
                Defaults to None.

        Returns:
            Dict[str, Union[str, float, np.ndarray]]: Post processed prediction results.
        """
        if meta_data is None:
            meta_data = self.meta_data

        if isinstance(predictions, Tensor):
            anomaly_map = predictions.detach().cpu().numpy()
            pred_score = anomaly_map.reshape(-1).max()
        else:
            # NOTE: Patchcore `forward`` returns heatmap and score.
            #   We need to add the following check to ensure the variables
            #   are properly assigned. Without this check, the code
            #   throws an error regarding type mismatch torch vs np.
            if isinstance(predictions[1], (Tensor)):
                anomaly_map, pred_score = predictions
                anomaly_map = anomaly_map.detach().cpu().numpy()
                pred_score = pred_score.detach().cpu().numpy()
            else:
                anomaly_map, pred_score = predictions
                pred_score = pred_score.detach()

        # Common practice in anomaly detection is to assign anomalous
        # label to the prediction if the prediction score is greater
        # than the image threshold.
        pred_label: Optional[str] = None
        if "image_threshold" in meta_data:
            pred_idx = pred_score >= meta_data["image_threshold"]
            pred_label = "Anomalous" if pred_idx else "Normal"

        pred_mask: Optional[np.ndarray] = None
        if "pixel_threshold" in meta_data:
            pred_mask = (anomaly_map >= meta_data["pixel_threshold"]).squeeze().astype(np.uint8)

        anomaly_map = anomaly_map.squeeze()
        anomaly_map, pred_score = self._normalize(anomaly_maps=anomaly_map, pred_scores=pred_score, meta_data=meta_data)

        if isinstance(anomaly_map, Tensor):
            anomaly_map = anomaly_map.detach().cpu().numpy()

        if "image_shape" in meta_data and anomaly_map.shape != meta_data["image_shape"]:
            image_height = meta_data["image_shape"][0]
            image_width = meta_data["image_shape"][1]
            anomaly_map = cv2.resize(anomaly_map, (image_width, image_height))

            if pred_mask is not None:
                pred_mask = cv2.resize(pred_mask, (image_width, image_height))

        if self.config.dataset.task == TaskType.DETECTION:
            pred_boxes = masks_to_boxes(torch.from_numpy(pred_mask))[0][0].numpy()
            box_labels = np.ones(pred_boxes.shape[0])
        else:
            pred_boxes = None
            box_labels = None

        return {
            "anomaly_map": anomaly_map,
            "pred_label": pred_label,
            "pred_score": pred_score,
            "pred_mask": pred_mask,
            "pred_boxes": pred_boxes,
            "box_labels": box_labels,
        }
