import csv
import os
import re
import time
import random
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


# =========================
# Config
# =========================

CITIES_LIST = [
    "valencia",
    "malaga",
    "alicante",
    "sevilla",
    "madrid",
    "barcelona",
    "las-palmas-de-gran-canaria",
    "tenerife-santa-cruz-la-laguna",
    "zaragoza",
    "a-coruna",
    "granada",
    "palma",
    "albacete",
    "jerez-de-la-frontera",
]

PRIMARY_TYPE = "comida_1"
EXTRA_TYPES_TO_TRY = []

MAX_GLOVO_STORES_PER_CITY = 5

# Para probar en Colab/local pon True.
# Para GitHub Actions pon False.
FORCE_RUN = False

PAUSE_CITY_S = 3.0
PAUSE_LIST_S = 2.0
PAUSE_STORE_S = 1.5

TIMEZONE = "Europe/Madrid"

# Ventana real cuando FORCE_RUN = False.
# Permite 00:00, 00:30, 01:00, 01:30, 02:00, 02:30 y 03:00.
REPARTO_PROPIO_MARKER = "El establecimiento entrega los pedidos directamente"

PCT_RE = re.compile(r"(-?\d{1,3})\s*%")


# =========================
# Control horario
# =========================

def inside_allowed_window() -> bool:
    if FORCE_RUN:
        print("FORCE_RUN=True. Ejecutando aunque esté fuera de la ventana horaria.")
        return True

    now = datetime.now(ZoneInfo(TIMEZONE))

    is_allowed = (
        now.hour in (0, 1, 2)
        or (now.hour == 3 and now.minute == 0)
    )

    if is_allowed:
        return True

    print(
        f"Fuera de ventana horaria. Hora local actual: "
        f"{now.strftime('%Y-%m-%d %H:%M:%S %Z')}. Saliendo sin scrapeo."
    )
    return False


# =========================
# Helpers de parseo
# =========================

def txt(el):
    return el.get_text(strip=True) if el else "-"


def startswith_class(prefix):
    def _m(c):
        if isinstance(c, list):
            return any(isinstance(x, str) and x.startswith(prefix) for x in c)
        return isinstance(c, str) and c.startswith(prefix)

    return _m


def bypref(soup, tag, pref):
    return soup.find(tag, class_=startswith_class(pref))


def allbypref(soup, tag, pref):
    return soup.find_all(tag, class_=startswith_class(pref))


def get_cards(soup):
    cards = []

    for a in soup.find_all("a", href=True):
        if a.find("p", class_=startswith_class("StoreCardStoreWall_title__")):
            cards.append(a)

    return cards


def extract_name(card):
    w = bypref(card, "h3", "StoreCardStoreWall_titleWrapper__")

    if w:
        p = w.find("p", class_=startswith_class("StoreCardStoreWall_title__")) or w.find("p")
        if p:
            return txt(p)

    p = card.find("p", class_=startswith_class("StoreCardStoreWall_title__"))

    return txt(p) if p else "-"


def extract_promos(card):
    promos = []
    prime = "No"
    pdisc = "-"
    nd = "-"

    pc = bypref(card, "div", "StoreCardStoreWall_promotion__")

    if pc:
        for b in allbypref(pc, "div", "StorePromotion_promotion__"):
            inner = b.find(True)
            t = txt(inner) or txt(b)

            if t and t != "-":
                promos.append(t)

    fee = bypref(card, "div", "StoreDeliveryFee_deliveryFee__")

    if fee:
        bf = fee.find("div", class_=startswith_class("StoreDeliveryFeeText_baseFee__"))

        if bf:
            ft = txt(bf)

            if ft and ft != "-":
                promos.append(ft)

        tag = fee.find("div", class_=startswith_class("Tag_pintxo-tag__"))

        if tag:
            tt = txt(tag)

            if tt and tt != "-":
                promos.append(tt)

    joined = " | ".join(promos) if promos else "-"

    if re.search(r"\bprime\b", joined, flags=re.I):
        prime = "Sí"

    for part in promos:
        m = PCT_RE.search(part)

        if m:
            pct = m.group(1)

            if prime == "Sí" and pdisc == "-":
                pdisc = pct
            elif nd == "-":
                nd = pct

    return joined, nd, prime, pdisc


def extract_rating_reviews(card):
    rating = "-"
    reviews = "-"

    wrap = bypref(card, "div", "StoreRatings_ratings__")

    if wrap:
        ps = wrap.find_all("p")

        if ps:
            rating = txt(ps[0]) or "-"

            if len(ps) > 1:
                reviews = txt(ps[1]) or "-"

    return rating, reviews


def abs_link(href):
    if not href:
        return "-"

    return href if href.startswith("http") else f"https://glovoapp.com{href}"


# =========================
# HTTP
# =========================

session = requests.Session()

session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
})


def fetch(url, referer=None, retries=3):
    headers = {}

    if referer:
        headers["Referer"] = referer

    backoff = 1.5

    for attempt in range(1, retries + 1):
        try:
            r = session.get(
                url,
                headers=headers,
                timeout=25,
                allow_redirects=True,
            )

            if r.status_code == 200:
                return r

            if r.status_code in (400, 403, 408, 429, 500, 502, 503, 504):
                sleep_s = backoff + random.uniform(0, 1.0)
                print(
                    f"  HTTP {r.status_code} en {url}. "
                    f"Retry {attempt}/{retries} en {sleep_s:.1f}s."
                )
                time.sleep(sleep_s)
                backoff *= 1.8
                continue

            return r

        except requests.RequestException as e:
            sleep_s = backoff + random.uniform(0, 1.0)
            print(
                f"  Error request en {url}: {e}. "
                f"Retry {attempt}/{retries} en {sleep_s:.1f}s."
            )
            time.sleep(sleep_s)
            backoff *= 1.8

    return None


# =========================
# Tipo de reparto
# =========================

def check_reparto_propio(store_url, referer=None):
    """
    Devuelve:
    - 'Propio' si la tienda hace su propio reparto.
    - 'Glovo' si no aparece el marker de reparto propio.
    - 'Error' si no se pudo obtener.
    """

    if store_url == "-":
        return "Desconocido"

    r = fetch(store_url, referer=referer)

    if not r:
        return "Error"

    if r.status_code != 200:
        return "Error"

    if REPARTO_PROPIO_MARKER in r.text:
        return "Propio"

    return "Glovo"


# =========================
# Descubridor de otras listas
# =========================

def discover_list_urls(landing_html, city, base_list_path):
    soup = BeautifulSoup(landing_html, "html.parser")
    found = set()

    prefix = f"/es/pt/{city}/"

    for a in soup.find_all("a", href=True):
        href = a["href"]

        if not href.startswith(prefix):
            continue

        if "?" in href or "#" in href:
            continue

        if not href.endswith("/"):
            continue

        if href == base_list_path:
            continue

        found.add(href)

    return sorted(found)


# =========================
# Procesador de cards
# =========================

def process_cards_until_5_glovo(
    cards,
    seen_links,
    writer,
    fecha,
    city,
    landing_url,
    glovo_count,
):
    """
    Procesa cards hasta llegar a MAX_GLOVO_STORES_PER_CITY.
    Solo escribe filas si Tipo de reparto == Glovo.
    """

    added_glovo = 0

    for c in cards:
        if glovo_count >= MAX_GLOVO_STORES_PER_CITY:
            break

        link = abs_link(c.get("href"))

        if link in seen_links:
            continue

        seen_links.add(link)

        nombre = extract_name(c)
        promo_text, nd, prime, pd = extract_promos(c)
        rating, reviews = extract_rating_reviews(c)

        reparto = check_reparto_propio(link, referer=landing_url)

        time.sleep(PAUSE_STORE_S + random.uniform(0, 0.7))

        if reparto != "Glovo":
            print(f"    Saltada: {nombre} | reparto={reparto}")
            continue

        writer.writerow([
            fecha,
            city,
            nombre,
            promo_text,
            nd,
            rating,
            reviews,
            "-",
            link,
            prime,
            pd,
            reparto,
        ])

        glovo_count += 1
        added_glovo += 1

        print(f"    Añadida Glovo #{glovo_count}: {nombre}")

    return glovo_count, added_glovo


# =========================
# Main
# =========================

def main():
    if not inside_allowed_window():
        return

    now = datetime.now(ZoneInfo(TIMEZONE))
    fecha_extraccion = now.strftime("%Y-%m-%d")

    output_dir = "outputs"
    os.makedirs(output_dir, exist_ok=True)

    output_file = os.path.join(output_dir, f"store_data_{fecha_extraccion}.csv")

    print(f"Output: {output_file}")

    with open(output_file, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)

        w.writerow([
            "Fecha de extracción",
            "Ciudad",
            "Nombre",
            "Promoción (texto)",
            "Descuento normal (%)",
            "Rating",
            "Reviews Number",
            "Categoria",
            "Link",
            "Prime",
            "Descuento prime",
            "Tipo de reparto",
        ])

        for city in CITIES_LIST:
            print(f"\n── Ciudad: {city} ──")

            seen_links = set()
            glovo_count = 0

            base_url = f"https://glovoapp.com/es/es/{city}/{PRIMARY_TYPE}/"
            base_list_path = f"/es/pt/{city}/{PRIMARY_TYPE}/"

            r = fetch(base_url)

            if not r or r.status_code != 200:
                status = r.status_code if r else "sin respuesta"
                print(f"  ✗ {status} en landing {PRIMARY_TYPE}")
                time.sleep(PAUSE_CITY_S)
                continue

            soup = BeautifulSoup(r.text, "html.parser")
            cards = get_cards(soup)

            print(f"  Landing {PRIMARY_TYPE}: {len(cards)} cards encontradas.")

            glovo_count, added = process_cards_until_5_glovo(
                cards=cards,
                seen_links=seen_links,
                writer=w,
                fecha=fecha_extraccion,
                city=city,
                landing_url=base_url,
                glovo_count=glovo_count,
            )

            print(f"  Landing: +{added} tiendas Glovo. Total ciudad: {glovo_count}")

            if glovo_count >= MAX_GLOVO_STORES_PER_CITY:
                print(f"  ✓ Objetivo alcanzado en {city}: {glovo_count} tiendas Glovo.")
                time.sleep(PAUSE_CITY_S + random.uniform(0, 1.0))
                continue

            discovered = discover_list_urls(
                landing_html=r.text,
                city=city,
                base_list_path=base_list_path,
            )

            for t in EXTRA_TYPES_TO_TRY:
                discovered.append(f"/es/pt/{city}/{t}/")

            discovered = sorted(set(discovered))

            for path in discovered:
                if glovo_count >= MAX_GLOVO_STORES_PER_CITY:
                    break

                url = urllib.parse.urljoin("https://glovoapp.com", path)

                resp = fetch(url, referer=base_url)

                if not resp or resp.status_code != 200:
                    continue

                soup2 = BeautifulSoup(resp.text, "html.parser")
                cards2 = get_cards(soup2)

                if not cards2:
                    continue

                print(f"  Lista {path}: {len(cards2)} cards encontradas.")

                glovo_count, new_here = process_cards_until_5_glovo(
                    cards=cards2,
                    seen_links=seen_links,
                    writer=w,
                    fecha=fecha_extraccion,
                    city=city,
                    landing_url=url,
                    glovo_count=glovo_count,
                )

                print(f"  Lista {path}: +{new_here} tiendas Glovo. Total ciudad: {glovo_count}")

                time.sleep(PAUSE_LIST_S + random.uniform(0, 1.0))

            if glovo_count < MAX_GLOVO_STORES_PER_CITY:
                print(f"  ⚠ Solo se encontraron {glovo_count} tiendas Glovo en {city}.")

            time.sleep(PAUSE_CITY_S + random.uniform(0, 1.0))

    print(f"\n✓ Scraping completado: {output_file}")


if __name__ == "__main__":
    main()