# -*- coding: utf-8 -*-
from enum import StrEnum


class ModelName(StrEnum):
    VEO = "veo-3"
    LUMA = "luma"


# Допустимые значения для параметров видео
ASPECT_CHOICES = ("16:9", "9:16")
SPEED_CHOICES = ("Fast", "Quality")
