"""Material tier definitions -- true-tone Liquid Glass.

Each tier encapsulates visual parameters for a clear glass surface:
refraction strength, micro-tint opacity (near-zero for true tone),
saturation boost, border treatment, shadow depth.

Key true-tone Liquid Glass principles:
  - Ultra-low tint_alpha (0.02-0.08): 92-98% of background color passes through
  - Edge-only refractive distortion: center ~80% area is clear passthrough
  - Specular highlight simulates light concentration through curved glass
  - No blur — Apple Liquid Glass is transparent, not frosted
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class MaterialTier:
    """True-tone Liquid Glass material tier."""

    name: str
    blur_radius: int            # Legacy (unused in true-tone path)
    downsample: int              # Legacy (unused)
    tint_alpha: float            # Micro-tint opacity (0.0-1.0, near-zero for true tone)
    saturation: float            # Vibrancy boost (1.0=off)
    border_alpha: int            # Border alpha (0-255)
    border_radius: int           # Corner radius (px)
    highlight_intensity: float   # Specular highlight (0.0-1.0)
    shadow_blur: int             # Shadow blur radius (px)
    shadow_offset_y: int         # Shadow Y offset (px)
    noise_total: float           # Base noise opacity (unused: GPU procedural)
    refraction: float = 0.03      # Edge refractive distortion strength


# ------------------------------------------------------------------
# Pre-built material tiers (true-tone Liquid Glass)
# ------------------------------------------------------------------

THIN = MaterialTier(
    name="thin",
    blur_radius=25,
    downsample=2,
    tint_alpha=0.02,
    saturation=1.15,
    border_alpha=46,
    border_radius=12,
    highlight_intensity=0.45,
    shadow_blur=24,
    shadow_offset_y=4,
    noise_total=0.008,
    refraction=0.02,
)
"""Thin true-tone glass -- secondary panels: 98% clear, subtle edge distortion."""

REGULAR = MaterialTier(
    name="regular",
    blur_radius=35,
    downsample=2,
    tint_alpha=0.01,
    saturation=1.0,
    border_alpha=64,
    border_radius=14,
    highlight_intensity=0.25,
    shadow_blur=32,
    shadow_offset_y=8,
    noise_total=0.012,
    refraction=0.08,
)
"""Regular true-tone glass -- primary sidebars: fiber-optic edge dispersion, 99% clear center."""

THICK = MaterialTier(
    name="thick",
    blur_radius=50,
    downsample=2,
    tint_alpha=0.08,
    saturation=1.4,
    border_alpha=77,
    border_radius=18,
    highlight_intensity=0.7,
    shadow_blur=48,
    shadow_offset_y=16,
    noise_total=0.03,
    refraction=0.06,
)
"""Thick true-tone glass -- modal dialogs: 92% clear, pronounced edge lensing."""
