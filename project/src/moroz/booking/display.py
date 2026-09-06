"""Display-only service names; provider identifiers and matching stay unchanged."""
import re


def service_display_name(name: str) -> str:
    name = " ".join(name.split())
    name = re.sub(r'"\s*([^"\n]+?)\s*"', r'«\1»', name)
    name = re.sub(r'\b(?:fresh|фреш)\s+(?:день|дня)\b', 'Фреш день', name, flags=re.I)
    name = re.sub(r'\bКОЛЛАГЕНАРИЙ\b', 'Коллагенарий', name)
    return re.sub(r'\bколлариум\b', 'Коллариум', name, flags=re.I)
