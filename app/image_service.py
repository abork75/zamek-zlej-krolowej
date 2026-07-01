from __future__ import annotations
import base64
import httpx
from pathlib import Path

from app.config import XAI_API_KEY, IMAGE_STYLE

IMG_DIR = Path(__file__).parent.parent / "frontend" / "img" / "locations"
IMG_DIR.mkdir(parents=True, exist_ok=True)


def image_path(file_id: str) -> Path:
    return IMG_DIR / f"{file_id}.png"


def resolve_variant(loc_id: str, location: dict, flags: dict) -> tuple[str, str]:
    """
    Zwraca (file_id, label) aktywnego wariantu obrazka.
    Warianty sprawdzane od góry — pierwszy pasujący wygrywa.
    Warunek null = domyślny (fallback).
    """
    variants = location.get("image_variants", [])
    if not variants:
        return loc_id, "(domyślny — brak wariantów w world.yaml)"

    for variant in variants:
        cond = variant.get("condition")
        if cond is None:
            return variant["file"], variant["label"]
        flag_val = flags.get(cond["flag"])
        if flag_val == cond["value"]:
            return variant["file"], variant["label"]

    return loc_id, "(fallback)"


def build_image_log(loc_id: str, location: dict, flags: dict) -> list[dict]:
    """Buduje audit log wariantów — dla debuggera."""
    variants = location.get("image_variants", [])
    log = []
    active_found = False

    for variant in variants:
        cond = variant.get("condition")
        if cond is None:
            active = not active_found
            reason = "domyślny (żaden warunek nie pasował)" if not active_found else "pominięty (wcześniejszy wariant aktywny)"
        else:
            flag_val = flags.get(cond["flag"], "(brak flagi)")
            matches = flag_val == cond["value"]
            active = matches and not active_found
            expected = cond["value"]
            match_str = "✓ pasuje" if matches else f"✗ oczekiwano: {expected}"
            reason = f"{cond['flag']} = '{flag_val}' {match_str}"

        if active:
            active_found = True

        log.append({
            "file": variant["file"],
            "label": variant["label"],
            "condition": cond,
            "reason": reason,
            "active": active,
            "cached": image_path(variant["file"]).exists(),
        })

    return log


def build_prompt(location: dict, prompt_extra: str) -> str:
    atmosphere = location.get("atmosphere", "")
    return f"{IMAGE_STYLE}, {prompt_extra}, {atmosphere}, square format"


async def generate_image(file_id: str, location: dict, prompt_extra: str, force: bool = False) -> bool:
    path = image_path(file_id)
    if path.exists() and not force:
        return True

    prompt = build_prompt(location, prompt_extra)

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.x.ai/v1/images/generations",
                headers={"Authorization": f"Bearer {XAI_API_KEY}"},
                json={
                    "model": "grok-imagine-image",
                    "prompt": prompt,
                    "n": 1,
                    "response_format": "b64_json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            b64 = data["data"][0]["b64_json"]
            path.write_bytes(base64.b64decode(b64))
            return True
    except Exception as e:
        print(f"[image_service] Błąd generacji dla {file_id}: {e}")
        return False
