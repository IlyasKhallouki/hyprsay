"""Speech to text: a local sherpa-onnx model, a gateway model, or local with a cloud rescue."""

from .gateway import GatewayRecognizer, SttAuthError, SttTimeout, SttUnavailable
from .hybrid import HybridRecognizer, make_recognizer
from .local import LocalRecognizer
from .models import ModelError, SttError, ensure

__all__ = [
    "GatewayRecognizer",
    "HybridRecognizer",
    "LocalRecognizer",
    "ModelError",
    "SttAuthError",
    "SttError",
    "SttTimeout",
    "SttUnavailable",
    "ensure",
    "make_recognizer",
]
