import random
from dataclasses import dataclass


@dataclass
class BiometricPersona:
    name: str
    seed: int
    archetype: str

    wpm: float
    typo_rate: float
    burstiness: float
    scatter_px: float
    fitts_precision: float
    reaction_ms: float
    dwell_scale: float
    curve_bias: float
    tremor_scale: float
    flick_vigor: float
    overshoot_rate: float
    fatigue_rate: float
    fling_bias: float
    curiosity: float
    patience: float

    @property
    def swipe_profile(self) -> dict:
        return dict(
            MOTION_PROFILES.get(
                self.archetype,
                MOTION_PROFILES["average"],
            )
        )

    @property
    def swipe_shape(self) -> str:
        return self.swipe_profile["shape"]

    @property
    def retry_budget(self) -> int:
        return 1 + round(self.patience * 4)


_BASE = {
    "wpm": (22.0, 58.0),
    "typo_rate": (0.015, 0.14),
    "burstiness": (1.0, 2.2),
    "scatter_px": (1.8, 6.2),
    "fitts_precision": (0.65, 1.30),
    "reaction_ms": (150.0, 330.0),
    "dwell_scale": (0.72, 1.40),
    "curve_bias": (0.30, 1.00),
    "tremor_scale": (0.60, 1.50),
    "flick_vigor": (0.82, 1.28),
    "overshoot_rate": (0.0, 0.32),
    "fatigue_rate": (0.0, 1.0),
    "fling_bias": (0.15, 0.85),
    "curiosity": (0.1, 0.9),
    "patience": (0.1, 0.9),
}


ARCHETYPES = {
    "speedy": {
        "wpm": (46, 62),
        "typo_rate": (0.05, 0.14),
        "burstiness": (1.6, 2.4),
        "reaction_ms": (140, 210),
        "dwell_scale": (0.76, 0.96),
        "fitts_precision": (0.85, 1.15),
        "tremor_scale": (0.55, 0.95),
        "flick_vigor": (1.05, 1.20),
        "overshoot_rate": (0.10, 0.30),
        "fling_bias": (0.70, 0.95),
        "patience": (0.15, 0.45),
        "curiosity": (0.15, 0.50),
    },
    "careful": {
        "wpm": (24, 40),
        "typo_rate": (0.01, 0.05),
        "burstiness": (1.0, 1.4),
        "reaction_ms": (240, 340),
        "dwell_scale": (1.15, 1.48),
        "fitts_precision": (1.05, 1.35),
        "tremor_scale": (0.55, 0.95),
        "flick_vigor": (0.78, 0.93),
        "overshoot_rate": (0.0, 0.10),
        "fling_bias": (0.10, 0.35),
        "patience": (0.60, 0.95),
        "curiosity": (0.20, 0.60),
    },
    "average": {
        "wpm": (32, 48),
        "typo_rate": (0.03, 0.09),
        "burstiness": (1.3, 1.9),
        "reaction_ms": (190, 280),
        "dwell_scale": (0.85, 1.15),
        "fitts_precision": (0.85, 1.15),
        "tremor_scale": (0.80, 1.20),
        "flick_vigor": (0.92, 1.08),
        "overshoot_rate": (0.05, 0.20),
        "fling_bias": (0.40, 0.65),
        "patience": (0.40, 0.70),
        "curiosity": (0.30, 0.65),
    },
    "shaky": {
        "wpm": (20, 34),
        "typo_rate": (0.08, 0.16),
        "burstiness": (1.0, 1.3),
        "reaction_ms": (260, 360),
        "dwell_scale": (1.10, 1.55),
        "fitts_precision": (0.60, 0.85),
        "scatter_px": (4.0, 7.0),
        "tremor_scale": (1.25, 1.75),
        "flick_vigor": (0.84, 0.98),
        "overshoot_rate": (0.15, 0.35),
        "fling_bias": (0.10, 0.30),
        "patience": (0.55, 0.90),
        "curiosity": (0.20, 0.55),
    },
    "restless": {
        "wpm": (34, 52),
        "typo_rate": (0.04, 0.12),
        "burstiness": (1.7, 2.4),
        "reaction_ms": (160, 250),
        "dwell_scale": (0.75, 1.05),
        "fitts_precision": (0.75, 1.05),
        "tremor_scale": (0.70, 1.15),
        "flick_vigor": (1.00, 1.16),
        "overshoot_rate": (0.15, 0.32),
        "fling_bias": (0.55, 0.85),
        "patience": (0.20, 0.50),
        "curiosity": (0.60, 0.95),
    },
    "careless": {
        "wpm": (28, 48),
        "typo_rate": (0.06, 0.15),
        "burstiness": (1.2, 2.0),
        "scatter_px": (3.0, 6.0),
        "fitts_precision": (0.60, 0.90),
        "reaction_ms": (190, 310),
        "dwell_scale": (0.80, 1.20),
        "curve_bias": (0.30, 0.85),
        "tremor_scale": (0.80, 1.40),
        "flick_vigor": (0.88, 1.06),
        "overshoot_rate": (0.10, 0.35),
        "fatigue_rate": (0.10, 0.80),
        "fling_bias": (0.35, 0.75),
        "patience": (0.15, 0.55),
        "curiosity": (0.30, 0.80),
    },
}


ARCHETYPE_NAMES = list(ARCHETYPES.keys())


MOTION_PROFILES = {
    "speedy": {
        "shape": "varied",
        "lateral_ratio": (0.006, 0.034, 0.082),
        "roughness": 0.22,
        "shape_weights": {
            "near_line": 0.25,
            "single_bend": 0.55,
            "shallow_s": 0.10,
            "wander": 0.10,
        },
        "duration_scale_bounds": (0.82, 1.04),
        "pressure_ratio": (0.50, 0.61, 0.73),
        "pressure_ripple": 0.035,
        "pressure_envelope": 0.18,
        "speed_curve_weights": {
            "smoothstep": 0.70,
            "ease_in_out_sine": 0.22,
            "ease_out_quad": 0.08,
        },
        "speed_curve": "smoothstep",
    },
    "careful": {
        "shape": "varied",
        "lateral_ratio": (0.004, 0.023, 0.060),
        "roughness": 0.15,
        "shape_weights": {
            "near_line": 0.43,
            "single_bend": 0.49,
            "shallow_s": 0.06,
            "wander": 0.02,
        },
        "duration_scale_bounds": (1.05, 1.38),
        "pressure_ratio": (0.76, 0.86, 0.95),
        "pressure_ripple": 0.018,
        "pressure_envelope": 0.10,
        "speed_curve_weights": {
            "ease_in_out_sine": 0.78,
            "smoothstep": 0.22,
        },
        "speed_curve": "ease_in_out_sine",
    },
    "average": {
        "shape": "varied",
        "lateral_ratio": (0.008, 0.050, 0.112),
        "roughness": 0.30,
        "shape_weights": {
            "near_line": 0.18,
            "single_bend": 0.52,
            "shallow_s": 0.16,
            "wander": 0.14,
        },
        "duration_scale_bounds": (0.90, 1.17),
        "pressure_ratio": (0.59, 0.72, 0.84),
        "pressure_ripple": 0.035,
        "pressure_envelope": 0.20,
        "speed_curve_weights": {
            "smoothstep": 0.65,
            "ease_in_out_sine": 0.30,
            "ease_out_quad": 0.05,
        },
        "speed_curve": "smoothstep",
    },
    "shaky": {
        "shape": "varied",
        "lateral_ratio": (0.014, 0.060, 0.120),
        "roughness": 0.56,
        "shape_weights": {
            "near_line": 0.10,
            "single_bend": 0.31,
            "shallow_s": 0.27,
            "wander": 0.32,
        },
        "duration_scale_bounds": (1.00, 1.28),
        "pressure_ratio": (0.59, 0.72, 0.86),
        "pressure_ripple": 0.075,
        "pressure_envelope": 0.22,
        "speed_curve_weights": {
            "ease_in_out_sine": 0.55,
            "smoothstep": 0.45,
        },
        "speed_curve": "ease_in_out_sine",
    },
    "restless": {
        "shape": "varied",
        "lateral_ratio": (0.010, 0.050, 0.110),
        "roughness": 0.48,
        "shape_weights": {
            "near_line": 0.12,
            "single_bend": 0.30,
            "shallow_s": 0.27,
            "wander": 0.31,
        },
        "duration_scale_bounds": (0.86, 1.14),
        "pressure_ratio": (0.53, 0.66, 0.80),
        "pressure_ripple": 0.052,
        "pressure_envelope": 0.19,
        "speed_curve_weights": {
            "smoothstep": 0.55,
            "ease_in_out_sine": 0.28,
            "ease_out_quad": 0.17,
        },
        "speed_curve": "smoothstep",
    },
    "careless": {
        "shape": "varied",
        "lateral_ratio": (0.012, 0.055, 0.120),
        "roughness": 0.50,
        "shape_weights": {
            "near_line": 0.14,
            "single_bend": 0.31,
            "shallow_s": 0.24,
            "wander": 0.31,
        },
        "duration_scale_bounds": (0.91, 1.24),
        "pressure_ratio": (0.56, 0.69, 0.83),
        "pressure_ripple": 0.060,
        "pressure_envelope": 0.22,
        "speed_curve_weights": {
            "ease_in_out_sine": 0.45,
            "smoothstep": 0.45,
            "ease_out_quad": 0.10,
        },
        "speed_curve": "ease_in_out_sine",
    },
}


def generate_persona(seed=None, archetype=None):
    """Create a reproducible persona with sampled trait values."""

    if seed is None:
        seed = random.randrange(1000, 9_999_999)

    seed = int(seed)
    rng = random.Random(seed)

    if archetype in (None, "", "auto"):
        archetype = ARCHETYPE_NAMES[
            seed % len(ARCHETYPE_NAMES)
        ]

    elif archetype == "random":
        archetype = rng.choice(ARCHETYPE_NAMES)

    elif archetype not in ARCHETYPES:
        raise ValueError(
            f"Unknown persona '{archetype}'. Available: "
            f"{', '.join(ARCHETYPE_NAMES)}, random"
        )

    ranges = dict(_BASE)
    ranges.update(ARCHETYPES[archetype])

    def sample(trait):
        low, high = ranges[trait]
        return rng.uniform(low, high)

    handedness = rng.choice([-1, 1])

    return BiometricPersona(
        name=f"{archetype}-{seed % 10000:04d}",
        seed=seed,
        archetype=archetype,
        wpm=sample("wpm"),
        typo_rate=sample("typo_rate"),
        burstiness=sample("burstiness"),
        scatter_px=sample("scatter_px"),
        fitts_precision=sample("fitts_precision"),
        reaction_ms=sample("reaction_ms"),
        dwell_scale=sample("dwell_scale"),
        curve_bias=handedness * sample("curve_bias"),
        tremor_scale=sample("tremor_scale"),
        flick_vigor=sample("flick_vigor"),
        overshoot_rate=sample("overshoot_rate"),
        fatigue_rate=sample("fatigue_rate"),
        fling_bias=sample("fling_bias"),
        curiosity=sample("curiosity"),
        patience=sample("patience"),
    )


def generate_random_persona() -> BiometricPersona:
    return generate_persona(None, "random")