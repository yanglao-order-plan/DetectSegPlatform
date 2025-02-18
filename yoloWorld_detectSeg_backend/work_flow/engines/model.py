import base64
import csv
import glob
import logging
import os
import pathlib
import binascii  # 新增导入
import cv2
import numpy as np
import yaml
import onnx
import urllib.request
from urllib.parse import urlparse
import re

import ssl

from PIL.Image import Image

from work_flow.utils.label_file import LabelFile, LabelFileError

from utils.backend_utils.colorprinter import print_red

ssl._create_default_https_context = (
    ssl._create_unverified_context
)  # Prevent issue when downloading flows behind a proxy

import socket

socket.setdefaulttimeout(240)  # Prevent timeout when downloading flows

from abc import abstractmethod

from .types import AutoLabelingResult

required_config_names = []
widgets = ["button_run"]
output_modes = {
    "rectangle": "Rectangle",
}
default_output_mode = "rectangle"

class Model:
    BASE_DOWNLOAD_URL = (
        "https://github.com/CVHub520/X-AnyLabeling/releases/tag"
    )
    # home_dir = os.path.expanduser("E:/models/yanglao")
    home_dir = os.path.expanduser("E:\models\yanglao")
    class Meta:
        required_config_names = []
        widgets = ["button_run"]
        output_modes = {
            "rectangle": "Rectangle",
        }
        default_output_mode = "rectangle"

    def __init__(self, model_config, on_message) -> None:
        super().__init__()
        self.on_message = on_message
        # Load and check config
        if isinstance(model_config, str):
            if not os.path.isfile(model_config):
                self.on_message("Config file not found: {model_config}"
                                .format(model_config=model_config))
            with open(model_config, "r") as f:
                self.config = yaml.safe_load(f)
        elif isinstance(model_config, dict):
            self.config = model_config
        else:
            self.on_message("Unknown config type: {type}".
                            format(type=type(model_config)))
        # self.check_missing_config(
        #     config_names=self.Meta.required_config_names,
        #     config=self.config,
        # )
        self.output_mode = self.Meta.default_output_mode

    def get_required_widgets(self):
        """
        Get required widgets for showing in UI
        """
        return self.Meta.widgets

    def allow_migrate_data(self):
        home_dir = os.path.expanduser("~")
        old_model_path = os.path.join(home_dir, "anylabeling_data")
        new_model_path = os.path.join(home_dir, "xanylabeling_data")

        if os.path.exists(new_model_path) or not os.path.exists(
            old_model_path
        ):
            return True

        # Check if the current env have write permissions
        if not os.access(home_dir, os.W_OK):
            return False

        # Attempt to migrate data
        try:
            os.rename(old_model_path, new_model_path)
            return True
        except Exception as e:
            self.on_message(f"An error occurred during data migration: {str(e)}")
            return False

    @staticmethod
    def check_model_shards(model_path):
        """
        检查模型分片文件是否存在且大小匹配，根据 model_path 目录下的任何 .csv 文件列出文件信息。

        参数:
        - model_path: str, 主模型文件路径，例如 'path/to/big-lama.pt'

        返回:
        - bool: 如果所有相关分片文件都存在且大小匹配，则返回 True，否则返回 False
        """
        # 获取目录路径和主文件的 basename（去掉扩展名）
        model_dir = os.path.dirname(model_path)
        base_name = os.path.splitext(os.path.basename(model_path))[0]

        # 搜索目录下的任何 .csv 文件
        csv_files = glob.glob(os.path.join(model_dir, '*.csv'))
        if not csv_files:
            print("未找到任何 .csv 文件。")
            return False

        # 在找到的 CSV 文件中查找分片信息
        shard_files = []
        for csv_file in csv_files:
            with open(csv_file, mode='r', encoding='utf-8') as file:
                reader = csv.DictReader(file)
                for row in reader:
                    filename = row['filename']
                    expected_filesize = int(row['filesize'])

                    # 过滤出与主文件 basename 匹配的分片文件
                    if filename.startswith(base_name):
                        shard_files.append((filename, expected_filesize))

        # 检查是否找到相关的分片文件
        if not shard_files:
            print(f"在 CSV 文件中未找到与 {base_name} 相关的分片文件。")
            return False

        # 检查每个分片文件是否存在以及文件大小是否匹配
        all_files_exist = True
        for filename, expected_filesize in shard_files:
            file_path = os.path.join(model_dir, filename)

            # 检查文件是否存在
            if not os.path.exists(file_path):
                print(f"文件 {filename} 不存在。")
                all_files_exist = False
                continue

            # 检查文件大小是否匹配
            actual_filesize = os.path.getsize(file_path)
            if actual_filesize != expected_filesize:
                print(f"文件 {filename} 大小不匹配。预期: {expected_filesize}字节，实际: {actual_filesize}字节")
                all_files_exist = False

        # 返回检查结果
        if all_files_exist:
            print("所有相关的模型分片文件都存在且大小匹配。")
        else:
            print("存在缺失或大小不匹配的模型分片文件。")

        return all_files_exist

    @staticmethod
    def convert_to_wsl_path(win_path):
        """
        将 Windows 路径转换为 WSL 路径。
        Args:
            win_path (str): Windows 文件路径 (例如 'C:\\path\\to\\file')。

        Returns:
            str: 转换后的 WSL 路径，如果路径无效则返回 None。
        """
        # 正则表达式匹配 Windows 路径格式（例如 C:\path\to\file）
        windows_path_pattern = r'^[a-zA-Z]:\\.*'

        # 检查输入是否符合 Windows 路径格式
        if re.match(windows_path_pattern, win_path):
            # 提取盘符并转换为小写（例如 "C:" -> "c"）
            drive_letter = win_path[0].lower()
            # 去除盘符，替换 \ 为 /，生成 WSL 路径
            path_without_drive = win_path[2:].replace('\\', '/')
            wsl_path = f"/mnt/{drive_letter}{path_without_drive}"

            # 打印或返回转换后的路径
            print(f"WSL path: {wsl_path}")
            return wsl_path
        else:
            print("Invalid Windows path format.")
            return None

    def get_model_abs_path(self, model_config, model_path_field_name):
        """
        Get model absolute path from config path or download from url
        """
        # Try getting model path from config folder
        model_path = model_config[model_path_field_name]
        local = model_path.get("local", None)
        online = model_path.get("online", None)
        # Continue with the rest of your function logic
        migrate_flag = self.allow_migrate_data()
        data_dir = "xanylabeling_data" if migrate_flag else "anylabeling_data"
        model_path = os.path.abspath(os.path.join(self.home_dir, data_dir))
        # Model path is a local path
        if local is not None and local.strip() != "":
            model_abs_path = local
            if (os.path.exists(model_abs_path)
                or self.check_model_shards(model_abs_path)
            ):
                return model_abs_path
            else:
                model_abs_path = self.convert_to_wsl_path(model_abs_path)
                if (os.path.exists(model_abs_path)
                        or self.check_model_shards(model_abs_path)
                ):
                    return model_abs_path
                else:
                    local_filename = os.path.basename(local)
                    local_model_abs_path = os.path.abspath(
                        os.path.join(
                            model_path,
                            "flows",
                            model_config["name"],
                            local_filename,
                        )
                    )
                    if os.path.exists(local_model_abs_path):
                        print(local_model_abs_path)
                        if local_model_abs_path.lower().endswith(".onnx"):
                            try:
                                onnx.checker.check_model(local_model_abs_path)
                            except onnx.checker.ValidationError as e:
                                self.on_message(f"{str(e)}")
                                self.on_message("Action: Delete and redownload...")
                                try:
                                    os.remove(local_model_abs_path)
                                except Exception as e:  # noqa
                                    self.on_message(f"Could not delete: {str(e)}")
                            else:
                                return local_model_abs_path
                        else:
                            return local_model_abs_path

            self.on_message("Model path not found: {model_path}".format(model_path=local))

        # Download model from url
        self.on_message("Downloading model from registry...")

        # Build download url
        def get_filename_from_url(url):
            a = urlparse(url)
            return os.path.basename(a.path)

        if online is not None and online.strip() != "":
            filename = get_filename_from_url(online)
            download_url = online

            model_abs_path = os.path.abspath(
                os.path.join(
                    model_path,
                    "flows",
                    model_config["name"],
                    filename,
                )
            )
            if os.path.exists(model_abs_path):
                if model_abs_path.lower().endswith(".onnx"):
                    try:
                        onnx.checker.check_model(model_abs_path)
                    except onnx.checker.ValidationError as e:
                        self.on_message(f"{str(e)}")
                        self.on_message("Action: Delete and redownload...")
                        try:
                            os.remove(model_abs_path)
                        except Exception as e:  # noqa
                            self.on_message(f"Could not delete: {str(e)}")
                    else:
                        return model_abs_path
                else:
                    return model_abs_path

            pathlib.Path(model_abs_path).parent.mkdir(parents=True, exist_ok=True)

            # Download url
            ellipsis_download_url = download_url
            if len(download_url) > 40:
                ellipsis_download_url = (
                    download_url[:20] + "..." + download_url[-20:]
                )

            self.on_message(f"Downloading {ellipsis_download_url} to {model_abs_path}")
            try:
                # Download and show progress
                def _progress(count, block_size, total_size):
                    percent = int(count * block_size * 100 / total_size)
                    self.on_message(
                        "Downloading {download_url}: {percent}%".format(
                            download_url=ellipsis_download_url, percent=percent
                        )
                    )
                urllib.request.urlretrieve(
                    download_url, model_abs_path, reporthook=_progress
                )
            except Exception as e:  # noqa
                self.on_message(f"Could not download {download_url}: {e}")
                return None
            return model_abs_path

        else:
            raise Exception("Model path local or online not found: {model_path}".format(model_path=local))

    def check_missing_config(self, config_names, config):
        """
        Check if config has all required config names
        """
        for name in config_names:
            if name not in config:
                raise Exception(f"Missing config: {name}")

    @abstractmethod
    def predict_shapes(self, image, filename=None) -> AutoLabelingResult:
        """
        Predict image and return AnyLabeling shapes
        """
        raise NotImplementedError

    @abstractmethod
    def unload(self):
        """
        Unload memory
        """
        raise NotImplementedError

    @staticmethod
    def load_image_from_filename(filename):
        """
        从标签文件或图像文件加载图像，并将其转换为 8 位 RGB 图像。

        Args:
            filename (str): 图像文件名。

        Returns:
            numpy.ndarray: 处理后的图像，格式为 8 位 RGB。
        """
        label_file_path = os.path.splitext(filename)[0] + ".json"
        image_data = None
        # 尝试从标签文件加载 imageData
        if os.path.exists(label_file_path) and LabelFile.is_label_file(label_file_path):
            try:
                label_file = LabelFile(label_file_path)
                if label_file.image_data is not None:
                    # 如果 imageData 存在，尝试解码 base64
                    try:
                        image_data = base64.b64decode(label_file.image_data)
                    except (binascii.Error, TypeError) as e:
                        logging.error(f"解码 base64 图像数据时出错，文件 {label_file_path}: {e}")
                        image_data = None
                elif label_file.image_path is not None:
                    # 如果 imageData 不存在，尝试从 imagePath 加载图像文件
                    image_path = os.path.join(os.path.dirname(label_file_path), label_file.image_path)
                    image_data = LabelFile.load_image_file(image_path)
            except LabelFileError as e:
                logging.error(f"读取标签文件 {label_file_path} 时出错: {e}")
                image_data = None
        # 如果无法从标签文件加载图像数据，直接从图像文件加载
        if image_data is None:
            try:
                image_data = LabelFile.load_image_file(filename)
            except Exception as e:
                logging.error(f"加载图像文件 {filename} 时出错: {e}")
                return None
        # 将图像数据转换为 NumPy 数组
        image_array = np.frombuffer(image_data, dtype=np.uint8)
        # 使用 OpenCV 解码图像
        cv_image = cv2.imdecode(image_array, cv2.IMREAD_UNCHANGED)
        if cv_image is None:
            logging.error(f"解码图像数据时出错，文件 {filename}")
            return None
        # 将图像转换为 8 位无符号整数类型
        if cv_image.dtype != np.uint8:
            cv_image = cv2.normalize(cv_image, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
        # 处理图像通道数，确保为 RGB 格式
        if len(cv_image.shape) == 2:
            # 灰度图像，转换为 RGB
            cv_image = cv2.cvtColor(cv_image, cv2.COLOR_GRAY2RGB)
        elif len(cv_image.shape) == 3:
            if cv_image.shape[2] == 1:
                # 单通道图像，转换为 RGB
                cv_image = cv2.cvtColor(cv_image, cv2.COLOR_GRAY2RGB)
            elif cv_image.shape[2] == 3:
                # BGR 图像，转换为 RGB
                cv_image = cv2.cvtColor(cv_image, cv2.COLOR_BGR2RGB)
            elif cv_image.shape[2] == 4:
                # BGRA 图像，转换为 RGB
                cv_image = cv2.cvtColor(cv_image, cv2.COLOR_BGRA2RGB)
            else:
                logging.error(f"不支持的图像格式，通道数为 {cv_image.shape[2]}。")
                return None
        else:
            logging.error("不支持的图像格式。")
            return None

        return cv_image

    def on_next_files_changed(self, next_files):
        """
        Handle next files changed. This function can preload next files
        and run inference to save time for user.
        """
        pass

    def set_output_mode(self, mode):
        """
        Set output mode
        """
        self.output_mode = mode
