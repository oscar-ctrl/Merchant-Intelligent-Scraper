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

# Cuántas tiendas revisar para decidir si la ciudad está "cerrada"
MAX_CARDS_TO_CHECK_CLOSED = 10

# Para probar en Colab/local pon True.
# Para GitHub Actions pon False.
FORCE_RUN = False

PAUSE_CITY_S = 3.0
PAUSE_LIST_S = 2.0
PAUSE_STORE_S = 1.5

TIMEZONE = "Europe/Madrid"

REPARTO_PROPIO_MARKER = "El establecimiento entrega los pedidos directamente"
CERRADO_MARKER = "aunque el establecimiento esté cerrado en el momento de hacer el pedido"

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
# Estado y tipo de reparto
# =========================

def check_store(store_url, referer=None):
    """
    Devuelve (reparto, estado):
      reparto: 'Propio' | 'Glovo' | 'Error' | 'Desconocido'
      estado:  'Abierto' | 'Cerrado' | 'Desconocido'
    """
    if store_url == "-":
        return "Desconocido", "Desconocido"

    r = fetch(store_url, referer=referer)

    if not r or r.status_code != 200:
        return "Error", "Desconocido"

    reparto = "Propio" if REPARTO_PROPIO_MARKER in r.text else "Glovo"
    estado = "Cerrado" if CERRADO_MARKER in r.text else "Abierto"

    return reparto, estado


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
# Recolector de cards de toda la ciudad
# =========================

def collect_all_city_cards(city, base_url, base_list_path, landing_html):
    """
    Recoge todas las cards únicas de la ciudad: landing + todas las sublistas.
    Devuelve lista de (card, source_url).
    """
    soup = BeautifulSoup(landing_html, "html.parser")
    landing_cards = get_cards(soup)

    all_cards = [(c, base_url) for c in landing_cards]
    seen_links = {abs_link(c.get("href")) for c in landing_cards}

    print(f"  Landing {PRIMARY_TYPE}: {len(landing_cards)} cards.")

    discovered = discover_list_urls(landing_html, city, base_list_path)

    for t in EXTRA_TYPES_TO_TRY:
        discovered.append(f"/es/pt/{city}/{t}/")

    discovered = sorted(set(discovered))

    for path in discovered:
        url = urllib.parse.urljoin("https://glovoapp.com", path)

        resp = fetch(url, referer=base_url)

        if not resp or resp.status_code != 200:
            continue

        soup2 = BeautifulSoup(resp.text, "html.parser")
        cards2 = get_cards(soup2)

        if not cards2:
            continue

        nuevas = 0
        for c in cards2:
            link = abs_link(c.get("href"))
            if link not in seen_links:
                seen_links.add(link)
                all_cards.append((c, url))
                nuevas += 1

        if nuevas:
            print(f"  Sublista {path}: +{nuevas} cards nuevas.")

        time.sleep(PAUSE_LIST_S + random.uniform(0, 1.0))

    print(f"  Total cards únicas en {city}: {len(all_cards)}")
    return all_cards


# =========================
# Comprobación de si la ciudad está cerrada
# =========================

def city_is_closed(all_cards, base_url):
    """
    Revisa hasta MAX_CARDS_TO_CHECK_CLOSED tiendas Glovo.
    Si todas están cerradas → devuelve True.
    Si alguna está abierta → devuelve False.
    """
    checked = 0

    for card, source_url in all_cards:
        if checked >= MAX_CARDS_TO_CHECK_CLOSED:
            break

        link = abs_link(card.get("href"))

        if link == "-":
            continue

        reparto, estado = check_store(link, referer=base_url)

        time.sleep(PAUSE_STORE_S + random.uniform(0, 0.5))

        if reparto != "Glovo":
            continue

        checked += 1
        print(f"    Check cierre #{checked}: {extract_name(card)} → {estado}")

        if estado == "Abierto":
            return False  # al menos una abierta → ciudad activa

    if checked == 0:
        print(f"  No se encontraron tiendas Glovo para verificar estado.")
        return True  # sin tiendas Glovo = tratar como cerrado

    print(f"  Las {checked} tiendas Glovo revisadas están cerradas.")
    return True


# =========================
# Procesador de cards
# =========================

def process_cards_until_5_glovo(
    all_cards,
    writer,
    fecha,
    hora_ejecucion,
    timestamp_ejecucion,
    city,
    base_url,
):
    """
    Recorre all_cards en orden hasta conseguir MAX_GLOVO_STORES_PER_CITY abiertas.
    Escribe solo tiendas Glovo + Abiertas.
    """
    glovo_count = 0
    seen_written = set()

    for card, source_url in all_cards:
        if glovo_count >= MAX_GLOVO_STORES_PER_CITY:
            break

        link = abs_link(card.get("href"))

        if link in seen_written:
            continue

        nombre = extract_name(card)
        promo_text, nd, prime, pd = extract_promos(card)
        rating, reviews = extract_rating_reviews(card)

        reparto, estado = check_store(link, referer=source_url)

        time.sleep(PAUSE_STORE_S + random.uniform(0, 0.7))

        if reparto != "Glovo":
            print(f"    Saltada (reparto propio): {nombre}")
            continue

        if estado != "Abierto":
            print(f"    Saltada (cerrada): {nombre}")
            continue

        seen_written.add(link)

        writer.writerow([
            fecha,
            hora_ejecucion,
            timestamp_ejecucion,
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
            estado,
        ])

        glovo_count += 1
        print(f"    Añadida Glovo abierta #{glovo_count}: {nombre}")

    return glovo_count


# =========================
# Main
# =========================

def main():
    if not inside_allowed_window():
        return

    now = datetime.now(ZoneInfo(TIMEZONE))

    fecha_extraccion = now.strftime("%Y-%m-%d")
    hora_ejecucion = now.strftime("%H:%M:%S")
    timestamp_ejecucion = now.strftime("%Y-%m-%d %H:%M:%S %Z")

    print(f"Hora de ejecución: {timestamp_ejecucion}")

    output_dir = "outputs"
    os.makedirs(output_dir, exist_ok=True)

    output_file = os.path.join(output_dir, f"store_data_{fecha_extraccion}.csv")

    print(f"Output: {output_file}")

    file_exists = os.path.exists(output_file)
    file_is_empty = (not file_exists) or os.path.getsize(output_file) == 0

    with open(output_file, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)

        if file_is_empty:
            w.writerow([
                "Fecha de extracción",
                "Hora ejecución",
                "Timestamp ejecución",
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
                "Estado",          # ← nueva columna
            ])

        for city in CITIES_LIST:
            print(f"\n── Ciudad: {city} ──")

            base_url = f"https://glovoapp.com/es/es/{city}/{PRIMARY_TYPE}/"
            base_list_path = f"/es/pt/{city}/{PRIMARY_TYPE}/"

            r = fetch(base_url)

            if not r or r.status_code != 200:
                status = r.status_code if r else "sin respuesta"
                print(f"  ✗ {status} en landing {PRIMARY_TYPE}")
                time.sleep(PAUSE_CITY_S)
                continue

            # 1. Recoger TODAS las cards de la ciudad (landing + sublistas)
            all_cards = collect_all_city_cards(
                city=city,
                base_url=base_url,
                base_list_path=base_list_path,
                landing_html=r.text,
            )

            if not all_cards:
                print(f"  ⚠ Sin cards en {city}. Saltando.")
                time.sleep(PAUSE_CITY_S)
                continue

            # 2. Comprobar si la ciudad está cerrada (primeras 10 Glovo)
            print(f"  Verificando estado de apertura en {city}...")
            if city_is_closed(all_cards, base_url):
                print(f"  ✗ {city} cerrada a esta hora. No se escriben filas.")
                time.sleep(PAUSE_CITY_S + random.uniform(0, 1.0))
                continue

            # 3. Procesar y escribir las tiendas abiertas
            glovo_count = process_cards_until_5_glovo(
                all_cards=all_cards,
                writer=w,
                fecha=fecha_extraccion,
                hora_ejecucion=hora_ejecucion,
                timestamp_ejecucion=timestamp_ejecucion,
                city=city,
                base_url=base_url,
            )

            if glovo_count >= MAX_GLOVO_STORES_PER_CITY:
                print(f"  ✓ Objetivo alcanzado en {city}: {glovo_count} tiendas Glovo abiertas.")
            else:
                print(f"  ⚠ Solo {glovo_count} tiendas Glovo abiertas encontradas en {city}.")

            time.sleep(PAUSE_CITY_S + random.uniform(0, 1.0))

    print(f"\n✓ Scraping completado en modo append: {output_file}")


if __name__ == "__main__":
    main()