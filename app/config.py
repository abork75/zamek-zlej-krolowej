from __future__ import annotations
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

XAI_API_KEY: str = os.environ.get("XAI_API_KEY", "")
GROK_MODEL: str  = os.environ.get("GROK_MODEL", "grok-3-fast")
GROK_VOICE: str  = os.environ.get("GROK_VOICE", "grok-voice-latest")
IMAGE_STYLE: str = os.environ.get("IMAGE_STYLE", "mroczna ilustracja fantasy, styl akwarela, bez tekstu, bez napisów")
IMAGE_STYLES: dict = {
    "akwarela": "mroczna ilustracja fantasy, styl akwarela, bez tekstu, bez napisów",
    "oil":      "dark fantasy, oil painting, dramatic lighting, bez tekstu, bez napisów",
    "pixel":    "pixel art 8-bit retro game, dark fantasy, bez tekstu, bez napisów",
    "sketch":   "pen and ink sketch, czarno-biały, gothic fantasy, bez tekstu, bez napisów",
    "bajkowy":  "watercolor fairy tale illustration, bright colors, bez tekstu, bez napisów",
}

_ROOT = Path(__file__).parent.parent
_GAMES_DIR = _ROOT / "games"
_ACTIVE_GAME_FILE = _ROOT / ".active_game"

def _read_active_game() -> str:
    if _ACTIVE_GAME_FILE.exists():
        name = _ACTIVE_GAME_FILE.read_text(encoding="utf-8").strip()
        if name and (_GAMES_DIR / name).is_dir():
            return name
    default = os.environ.get("ACTIVE_GAME", "zamek-dev")
    return default

_active_game: str = _read_active_game()

def get_game_dir() -> Path:
    return _GAMES_DIR / _active_game

def get_active_game() -> str:
    return _active_game

def set_active_game(name: str) -> bool:
    global _active_game
    target = _GAMES_DIR / name
    if not target.is_dir():
        return False
    _active_game = name
    _ACTIVE_GAME_FILE.write_text(name, encoding="utf-8")
    return True

def list_games() -> list[str]:
    if not _GAMES_DIR.exists():
        return []
    return sorted(d.name for d in _GAMES_DIR.iterdir() if d.is_dir())
