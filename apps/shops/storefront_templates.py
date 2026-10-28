"""Storefront design presets. Applying one sets the look of the store (colours, font, cover
layout, logo size) in one step; merchant content such as the logo, cover photo, and copy is
never touched, and every value can still be fine-tuned afterwards in the settings form."""

STOREFRONT_TEMPLATES = {
    "retail": {
        "label": "Retail Oranye",
        "description": "Energik dan promosi-sentris untuk toko serba ada.",
        "values": {"theme": "modern", "primary_color": "#f97316", "background_color": "#fffaf5",
            "font_family": "sans", "cover_layout": "split", "logo_size": "medium",
            "cover_overlay": 45},
    },
    "minimal": {
        "label": "Minimalis Monokrom",
        "description": "Bersih dan tenang untuk fesyen atau produk desain.",
        "values": {"theme": "modern", "primary_color": "#111827", "background_color": "#ffffff",
            "font_family": "sans", "cover_layout": "centered", "logo_size": "medium",
            "cover_overlay": 35},
    },
    "boutique": {
        "label": "Butik Hangat",
        "description": "Foto sampul penuh dan huruf serif untuk brand premium.",
        "values": {"theme": "warm", "primary_color": "#b45309", "background_color": "#fdf8f3",
            "font_family": "serif", "cover_layout": "image", "logo_size": "large",
            "cover_overlay": 45},
    },
    "fresh": {
        "label": "Segar & Natural",
        "description": "Hijau ramah untuk makanan, kecantikan, dan kesehatan.",
        "values": {"theme": "modern", "primary_color": "#059669", "background_color": "#f6fbf8",
            "font_family": "rounded", "cover_layout": "split", "logo_size": "medium",
            "cover_overlay": 40},
    },
    "tech": {
        "label": "Elektronik Modern",
        "description": "Biru tegas dengan foto sampul untuk gadget dan katalog besar.",
        "values": {"theme": "modern", "primary_color": "#2563eb", "background_color": "#f8fafc",
            "font_family": "sans", "cover_layout": "image", "logo_size": "small",
            "cover_overlay": 55},
    },
}


def apply_storefront_template(store, key):
    preset = STOREFRONT_TEMPLATES[key]
    for field, value in preset["values"].items():
        setattr(store, field, value)
    store.storefront_template = key
    store.save(update_fields=[*preset["values"], "storefront_template"])
    return preset
