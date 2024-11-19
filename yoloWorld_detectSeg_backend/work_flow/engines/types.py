import logging


class AutoLabelingResult:
    def __init__(self, shapes, replace=True, description="", image=None, visible=True, avatars=[], **kwargs):
        """Initialize AutoLabelingResult

        Args:
            shapes (List[Shape]): List of shapes to add to the canvas.
            replace (bool, optional): Replaces all current shapes with
            new shapes. Defaults to True.
            description (str, optional): Description of the image.
            Defaults to "".
        """

        self.shapes = shapes
        self.replace = replace
        self.description = description
        self.image = image
        self.avatars = avatars
        self.visible = visible

        # 通知canvas
        self.kwargs = kwargs

    def check_shapes(self):
        for shape in self.shapes:
            logging.info(shape.to_dict())

    def load_kwargs(self, **kwargs):
        self.kwargs.update(kwargs)


class AutoLabelingMode:
    OBJECT = "AUTOLABEL_OBJECT"
    ADD = "AUTOLABEL_ADD"
    REMOVE = "AUTOLABEL_REMOVE"
    POINT = "point"
    RECTANGLE = "rectangle"

    def __init__(self, edit_mode, shape_type):
        """Initialize AutoLabelingMode

        Args:
            edit_mode (str): AUTOLABEL_ADD / AUTOLABEL_REMOVE
            shape_type (str): point / rectangle
        """

        self.edit_mode = edit_mode
        self.shape_type = shape_type

    @staticmethod
    def get_default_mode():
        """Get default mode"""
        return AutoLabelingMode(AutoLabelingMode.ADD, AutoLabelingMode.POINT)

    # Compare 2 instances of AutoLabelingMode
    def __eq__(self, other):
        if not isinstance(other, AutoLabelingMode):
            return False
        return (
            self.edit_mode == other.edit_mode
            and self.shape_type == other.shape_type
        )
