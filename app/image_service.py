from __future__ import annotations
import base64
import httpx
from pathlib import Path

from app.config import XAI_API_KEY, IMAGE_STYLE

IMG_DIR = Path(__file__).parent.parent / "frontend" / "img" / "locations"
IMG_DIR.mkdir(parents=True, exist_ok=True)


def image_path(file_id: str) -> Path:
    return IMG_DIR / f"{file_id}.png"


def _check_image_condition(cond, flags: dict, inventory: list) -> bool:
    """
    Sprawdza warunek wariantu obrazka.
    cond może być:
      - None → zawsze True (fallback)
      - dict → jeden warunek
      - list[dict] → AND wszystkich warunków
    """
    if cond is None:
        return True
    if isinstance(cond, list):
        return all(_check_image_condition(c, flags, inventory) for c in cond)

    # flag check
    if "flag" in cond:
        flag_val = flags.get(cond["flag"])
        if "values" in cond:
            if flag_val not in cond["values"]:
                return False
        elif "value_not" in cond:
            if flag_val == cond["value_not"]:
                return False
        elif flag_val != cond.get("value"):
            return False

    # inventory checks
    if "inventory_contains" in cond:
        if cond["inventory_contains"] not in inventory:
            return False
    if "inventory_missing" in cond:
        if cond["inventory_missing"] in inventory:
            return False

    return True


def resolve_variant(loc_id: str, location: dict, flags: dict, inventory: list | None = None) -> tuple[str, str]:
    """
    Zwraca (file_id, label) aktywnego wariantu obrazka.
    Warianty sprawdzane od góry — pierwszy pasujący wygrywa.
    Warunek null = domyślny (fallback).
    """
    inventory = inventory or []
    variants = location.get("image_variants", [])
    if not variants:
        return loc_id, "(domyślny — brak wariantów w world.yaml)"

    for variant in variants:
        cond = variant.get("condition")
        if cond is None or _check_image_condition(cond, flags, inventory):
            return variant["file"], variant["label"]

    return loc_id, "(fallback)"


def _find_base_variant(location: dict) -> dict | None:
    """Zwraca wariant oznaczony base: true, lub null-condition jako fallback."""
    variants = location.get("image_variants", [])
    for v in variants:
        if v.get("base"):
            return v
    # fallback: wariant z condition: null
    for v in variants:
        if v.get("condition") is None:
            return v
    return None


def build_image_log(loc_id: str, location: dict, flags: dict, inventory: list | None = None) -> list[dict]:
    """Buduje audit log wariantów — dla debuggera."""
    inventory = inventory or []
    variants = location.get("image_variants", [])
    result = []
    active_found = False

    for variant in variants:
        cond = variant.get("condition")
        if cond is None:
            active = not active_found
            reason = "domyślny (żaden warunek nie pasował)" if not active_found else "pominięty (wcześniejszy wariant aktywny)"
        else:
            matches = _check_image_condition(cond, flags, inventory)
            active = matches and not active_found
            reason = "✓ pasuje" if matches else f"✗ nie pasuje: {cond}"

        if active:
            active_found = True

        result.append({
            "file": variant["file"],
            "label": variant["label"],
            "condition": cond,
            "reason": reason,
            "active": active,
            "cached": image_path(variant["file"]).exists(),
            "base": variant.get("base", False),
        })

    return result


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
        print(f"[image_service] Błąd generacji T2I dla {file_id}: {e}")
        return False


async def generate_image_i2i(
    file_id: str,
    base_path: Path,
    location: dict,
    prompt_extra: str,
    force: bool = False,
) -> bool:
    path = image_path(file_id)
    if path.exists() and not force:
        return True

    if not base_path.exists():
        print(f"[image_service] Baza I2I nie istnieje: {base_path} — fallback T2I")
        return await generate_image(file_id, location, prompt_extra, force=force)

    prompt = build_prompt(location, prompt_extra)
    b64_base = base64.b64encode(base_path.read_bytes()).decode()
    data_url = f"data:image/png;base64,{b64_base}"

    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                "https://api.x.ai/v1/images/edits",
                headers={"Authorization": f"Bearer {XAI_API_KEY}"},
                json={
                    "model": "grok-imagine-image-quality",
                    "prompt": prompt,
                    "image": {"url": data_url},
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
        print(f"[image_service] Błąd generacji I2I dla {file_id}: {e} — fallback T2I")
        return await generate_image(file_id, location, prompt_extra, force=force)
