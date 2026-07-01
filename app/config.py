from __future__ import annotations
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

XAI_API_KEY: str = os.environ.get("XAI_API_KEY", "")
GROK_MODEL: str = os.environ.get("GROK_MODEL", "grok-3-fast")
GROK_VOICE: str = os.environ.get("GROK_VOICE", "grok-voice-latest")
IMAGE_STYLE: str = os.environ.get("IMAGE_STYLE", "mroczna ilustracja fantasy, styl akwarela, bez tekstu, bez napisów")
IMAGE_STYLES: dict = {
    "akwarela":  "mroczna ilustracja fantasy, styl akwarela, bez tekstu, bez napisów",
    "oil":       "dark fantasy, oil painting, dramatic lighting, bez tekstu, bez napisów",
    "pixel":     "pixel art 8-bit retro game, dark fantasy, bez tekstu, bez napisów",
    "sketch":    "pen and ink sketch, czarno-biały, gothic fantasy, bez tekstu, bez napisów",
    "bajkowy":   "watercolor fairy tale illustration, bright colors, bez tekstu, bez napisów",
}
