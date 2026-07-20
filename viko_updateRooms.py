"""
ByCS ViKo (Visavid) - Automatischer Moderator-Beitritt
======================================================
Loggt sich per ByCS-SSO ein, navigiert in die Dauerraum-Liste, betritt den
angezeigten Raum als Moderator*in und tritt der Konferenz mit hinterlegtem
Anzeigenamen bei.

Aufbau bewusst analog zum Schwesterprojekt "ByCS_Backup" (gleicher
Retry-Decorator, gleiche load_config-/create_webdriver-Struktur).

Konfiguration (config/.env.Viko):
    MEBIS_USERNAME=dein_username
    MEBIS_PASSWORD=dein_passwort
    VIKO_DISPLAY_NAME=Herr Nachname
    # VIKO_ROOM_URL=https://viko.bycs.de/v/0582-1624-3704   # optional, ueberspringt Raumliste
    # VIKO_ROOM_NAME=AEuP12                                  # optional, Filter bei mehreren Raeumen
    MODE_HEADLESS=False
    KEEP_OPEN=True

Nutzung:
    python scripts/viko_enter.py
"""

import functools
import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    TimeoutException,
    WebDriverException,
    NoSuchElementException,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Konstanten / Selektoren
# ---------------------------------------------------------------------------
AUTH_URL = (
    "https://auth.bycs.de/realms/bycs/protocol/openid-connect/auth"
    "?client_id=visavid-oidc&scope=openid&response_type=code"
    "&redirect_uri=https%3A%2F%2Fviko.bycs.de%2Fapp%2Fredirect%2Fmebis"
)
ROOMS_URL = "https://viko.bycs.de/app/raeume"
WAITTIME = 15

# Login-Formulare: ByCS-Anmeldeportal zuerst, Keycloak-Default als Fallback.
# Jede Zeile: (user_by, user_val, pass_by, pass_val, submit_by, submit_val)
LOGIN_FORMS = [
    (By.ID, "input-username", By.ID, "input-password", By.ID, "button-do-log-in"),
    (By.ID, "username",       By.ID, "password",       By.ID, "kc-login"),
]

# --- Raumliste (verifiziert am echten Angular-DOM) --------------------------
# Tab-Umschaltung erfolgt clientseitig, der ?type=dauerhaft-Param genuegt nicht.
XP_TAB_DAUERRAEUME = "//a[@act-tab-nav][normalize-space(.)='Dauerhafte Räume']"
CSS_ROOM_ITEM      = "act-gen-list-item"                  # ein Raum = ein List-Item
CSS_JOIN_BTN       = "button.btnBeitreten"                # 'Raum betreten' im Item
CSS_ROOM_NAMEFIELD = "div.fieldText"                      # erstes fieldText = Raumname

# --- Moderator-Dialog (Raumliste) -------------------------------------------
# Dieser Zwischendialog liegt mir nicht als DOM vor -> text-basiert, tolerant.
XP_ALS_MODERATOR   = "//*[self::button or self::a][contains(normalize-space(.), 'Als Moderator betreten')]"

# --- /v/-Beitrittsseite (verifiziert am echten DOM) -------------------------
# Achtung: der Button heisst "Raum als Moderator BETRETEN" (nicht "beitreten").
CSS_JOIN_SUBMIT    = "button.test-login-button"          # Final-Submit, stabiler Test-Hook
XP_JOIN_SUBMIT     = "//button[@type='submit'][contains(normalize-space(.), 'Raum als Moderator betreten')]"

# Namensfeld: <input> in <visavid-input-name>, placeholder='Name', maxlength=40.
NAME_INPUT_CANDIDATES = [
    (By.CSS_SELECTOR, "visavid-input-name input"),
    (By.CSS_SELECTOR, "input.test-name-input-login-mod"),
    (By.CSS_SELECTOR, "input[placeholder='Name']"),
    (By.CSS_SELECTOR, "input[placeholder*='Name' i]"),
    (By.CSS_SELECTOR, "input[type='text']"),
]

# Checkboxen der Beitrittsseite werden ueber ihren Label-Text gesteuert.
CB_REUSE_NAME_LABEL = "wiederverwenden"                   # Name bei naechster Sitzung wiederverwenden
CB_MUTE_OUTPUT_LABEL = "Ton-Ausgabe"                     # Ton-Ausgabe bei Betreten deaktivieren
# Pflicht-Checkbox: Nutzungsbedingungen (muss immer gesetzt sein, stabile Klasse).
CSS_TERMS_CHECKBOX = "input.visavidTermsOfUseCheckbox"


# ---------------------------------------------------------------------------
# Retry-Decorator (identisch zum Backup-Projekt)
# ---------------------------------------------------------------------------
def retry(max_retries=3, base_delay=2.0, backoff_factor=2.0,
          exceptions=(TimeoutException, WebDriverException)):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        delay = base_delay * (backoff_factor ** attempt)
                        logger.warning(
                            f"[RETRY] {func.__name__} Versuch {attempt+1}/{max_retries} "
                            f"fehlgeschlagen: {type(e).__name__}. Warte {delay:.1f}s..."
                        )
                        time.sleep(delay)
            raise last_exception
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Konfiguration laden
# ---------------------------------------------------------------------------
def load_config(env_path: str = None) -> dict:
    if env_path is None:
        script_dir = Path(__file__).resolve().parent
        candidates = [
            script_dir / ".env.Viko",
            script_dir.parent / ".env.Viko",
            script_dir.parent / "config" / ".env.Viko",
        ]
        for c in candidates:
            if c.exists():
                env_path = str(c)
                break

    # Bereits gesetzte OS-Umgebungsvariablen sichern -> sie haben Vorrang vor
    # der Datei (Credentials koennen wahlweise als Umgebungsvariable ODER in
    # der .env.Viko stehen, analog zum Backup-Script).
    env_user = os.environ.get("MEBIS_USERNAME")
    env_pass = os.environ.get("MEBIS_PASSWORD")

    if env_path:
        load_dotenv(env_path, override=True)
        logger.info(f"Konfiguration geladen: {env_path}")
    else:
        logger.warning(".env.Viko nicht gefunden -> nutze ausschliesslich Umgebungsvariablen.")

    # Umgebungsvariable schlaegt Dateiwert.
    username = env_user or os.getenv("MEBIS_USERNAME")
    password = env_pass or os.getenv("MEBIS_PASSWORD")
    if not username or not password:
        raise ValueError(
            "MEBIS_USERNAME und MEBIS_PASSWORD fehlen. Entweder als "
            "Umgebungsvariable setzen oder in .env.Viko eintragen."
        )
    src = "Umgebungsvariable" if env_user and env_pass else "Datei/Umgebung"
    logger.info(f"Login-Daten bezogen aus: {src}")

    display_name = os.getenv("VIKO_DISPLAY_NAME", "").strip()
    if not display_name:
        raise ValueError("VIKO_DISPLAY_NAME muss gesetzt sein (Anzeigename im Raum).")

    return {
        "username": username,
        "password": password,
        "display_name": display_name,
        "room_url": os.getenv("VIKO_ROOM_URL", "").strip(),
        "room_name": os.getenv("VIKO_ROOM_NAME", "").strip(),
        "headless": os.getenv("MODE_HEADLESS", "False"),
        "keep_open": os.getenv("KEEP_OPEN", "True"),
        "fake_media": os.getenv("USE_FAKE_MEDIA", "True"),
        "reuse_name": os.getenv("VIKO_REUSE_NAME", "True") == "True",
        "mute_output": os.getenv("VIKO_MUTE_OUTPUT", "False") == "True",
    }


# ---------------------------------------------------------------------------
# WebDriver
# ---------------------------------------------------------------------------
def create_webdriver(headless: str = "False", fake_media: str = "True") -> webdriver.Chrome:
    options = Options()

    if headless == "True":
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--log-level=3")

    # getUserMedia-Prompt automatisch bestaetigen.
    options.add_argument("--use-fake-ui-for-media-stream")
    # Fake-Kamera/-Mikro: noetig bei mehreren parallelen Konferenz-Tabs, da eine
    # echte Kamera nicht gleichzeitig von mehreren Tabs genutzt werden kann.
    if fake_media == "True":
        options.add_argument("--use-fake-device-for-media-stream")
    options.add_experimental_option("prefs", {
        "profile.default_content_setting_values.media_stream_mic": 1,
        "profile.default_content_setting_values.media_stream_camera": 1,
        "profile.default_content_setting_values.notifications": 1,
    })

    return webdriver.Chrome(options=options)


# ---------------------------------------------------------------------------
# Helfer
# ---------------------------------------------------------------------------
def wait_for_page_load(driver, timeout: int = WAITTIME):
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )


def wait_and_click(driver, by, value, description: str = "", timeout: int = WAITTIME):
    desc = description or value
    element = WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((by, value))
    )
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    try:
        element.click()
    except ElementClickInterceptedException:
        driver.execute_script("arguments[0].click();", element)
    logger.info(f"  Geklickt: {desc}")
    return element


# ---------------------------------------------------------------------------
# Login (SSO ueber OIDC-Auth-URL)
# ---------------------------------------------------------------------------
@retry(max_retries=3, base_delay=2.0)
def login(driver, username: str, password: str):
    driver.get(AUTH_URL)
    wait_for_page_load(driver)

    # Passendes Login-Formular finden (ByCS-Portal oder Keycloak).
    form = None
    for (u_by, u_val, p_by, p_val, s_by, s_val) in LOGIN_FORMS:
        try:
            user_field = WebDriverWait(driver, 8).until(
                EC.presence_of_element_located((u_by, u_val))
            )
            form = (user_field, p_by, p_val, s_by, s_val)
            logger.info(f"  Login-Formular erkannt ueber {u_by}='{u_val}'")
            break
        except TimeoutException:
            continue

    if form is None:
        # Evtl. bereits aus vorheriger Session eingeloggt und direkt durchgereicht.
        if "viko.bycs.de" in driver.current_url:
            logger.info("Bereits angemeldet (bestehende Session).")
            return
        raise WebDriverException(
            f"Kein bekanntes Login-Formular gefunden. Aktuelle URL: {driver.current_url}"
        )

    user_field, p_by, p_val, s_by, s_val = form
    user_field.clear()
    user_field.send_keys(username)

    password_field = driver.find_element(p_by, p_val)
    password_field.clear()
    password_field.send_keys(password)

    driver.find_element(s_by, s_val).click()

    # Zurueck-Redirect nach viko.bycs.de abwarten.
    WebDriverWait(driver, WAITTIME).until(
        lambda d: "auth.bycs.de" not in d.current_url
    )
    logger.info(f"Login erfolgreich. URL: {driver.current_url}")


# ---------------------------------------------------------------------------
# Raum als Moderator*in betreten
# ---------------------------------------------------------------------------
def go_to_dauerraeume(driver):
    """Oeffnet die Raumverwaltung und schaltet auf den Tab 'Dauerhafte Räume'."""
    driver.get(ROOMS_URL)
    wait_for_page_load(driver)
    wait_and_click(driver, By.XPATH, XP_TAB_DAUERRAEUME, "Tab 'Dauerhafte Räume'")
    # Warten bis die Liste (mind. ein Item mit Beitreten-Button) gerendert ist.
    WebDriverWait(driver, WAITTIME).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, f"{CSS_ROOM_ITEM} {CSS_JOIN_BTN}"))
    )
    logger.info("Tab 'Dauerhafte Räume' aktiv, Raumliste geladen.")


def list_rooms(driver):
    """Liefert [(raumname, item_element), ...] aus der aktuellen Liste."""
    rooms = []
    for item in driver.find_elements(By.CSS_SELECTOR, CSS_ROOM_ITEM):
        try:
            # Erstes fieldText = Raumname (zweites waere der Owner).
            name_field = item.find_element(By.CSS_SELECTOR, CSS_ROOM_NAMEFIELD)
            name = (name_field.get_attribute("title") or name_field.text).strip()
        except NoSuchElementException:
            name = ""
        rooms.append((name, item))
    logger.info(f"Gefundene Dauerraeume: {[r[0] for r in rooms]}")
    return rooms


def click_join(driver, item, name: str = ""):
    """Klickt 'Raum betreten' innerhalb eines Raum-Items."""
    btn = item.find_element(By.CSS_SELECTOR, CSS_JOIN_BTN)
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
    try:
        btn.click()
    except ElementClickInterceptedException:
        driver.execute_script("arguments[0].click();", btn)
    logger.info(f"  'Raum betreten' geklickt (Raum: {name or 'n/a'})")


def open_room_dialog(driver, room_name: str = ""):
    """Waehlt Zielraum (per Name oder ersten) und klickt 'Raum betreten'."""
    go_to_dauerraeume(driver)
    rooms = list_rooms(driver)
    if not rooms:
        raise WebDriverException("Keine Dauerraeume in der Liste gefunden.")

    target = None
    if room_name:
        for name, item in rooms:
            if name == room_name or room_name.lower() in name.lower():
                target = (name, item)
                break
        if target is None:
            logger.warning(f"  Raum '{room_name}' nicht gefunden, nutze ersten Raum.")
    if target is None:
        target = rooms[0]

    click_join(driver, target[1], target[0])


def choose_moderator_in_dialog(driver):
    """Im Dialog 'Als Moderator betreten' waehlen. Tolerant: faellt der Dialog
    aus (z.B. bei gesetztem Moderator-Einwahlcode -> direkt /v/), wird er
    uebersprungen."""
    before = set(driver.window_handles)
    try:
        wait_and_click(driver, By.XPATH, XP_ALS_MODERATOR, "Als Moderator betreten", timeout=8)
        _switch_to_new_window(driver, before, timeout=8)
    except TimeoutException:
        logger.info("  Kein Moderator-Dialog erschienen, fahre direkt fort.")
    WebDriverWait(driver, WAITTIME).until(
        lambda d: "/v/" in d.current_url or _find_name_input(d) is not None
    )
    logger.info(f"Beitrittsseite: {driver.current_url}")


def _switch_to_new_window(driver, before_handles, timeout=8):
    """Wechselt in ein neu geoeffnetes Fenster/Tab, falls eines aufging."""
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: len(d.window_handles) > len(before_handles)
        )
        new = [h for h in driver.window_handles if h not in before_handles]
        if new:
            driver.switch_to.window(new[-1])
            logger.info("  Neues Fenster erkannt, dorthin gewechselt.")
    except TimeoutException:
        pass  # Same-Tab-Navigation, kein Fensterwechsel noetig


def join_single_room(driver, room_name: str, display_name: str,
                     reuse_name: bool = True, mute_output: bool = False) -> str:
    """Kompletter Beitritt fuer EINEN Raum im aktuellen Tab.
    Rueckgabe: Window-Handle des Konferenz-Tabs."""
    open_room_dialog(driver, room_name)
    choose_moderator_in_dialog(driver)
    join_as_moderator(driver, display_name, reuse_name, mute_output)
    return driver.current_window_handle


def _find_name_input(driver):
    for by, val in NAME_INPUT_CANDIDATES:
        try:
            el = driver.find_element(by, val)
            if el.is_displayed() and el.is_enabled():
                return el
        except NoSuchElementException:
            continue
    return None


def _set_checkbox_by_label(driver, label_substring: str, desired: bool):
    """Setzt genau die Checkbox, deren Label den Text enthaelt, auf desired."""
    xp = (
        f"//label[contains(normalize-space(.), '{label_substring}')]"
        f"//input[@type='checkbox'] | "
        f"//input[@type='checkbox'][following-sibling::text()[contains(., '{label_substring}')]]"
    )
    boxes = driver.find_elements(By.XPATH, xp)
    if not boxes:
        logger.warning(f"  Checkbox '{label_substring}' nicht gefunden (uebersprungen).")
        return
    cb = boxes[0]
    if cb.is_selected() == desired:
        logger.info(f"  Checkbox '{label_substring}': bereits {desired}.")
        return
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", cb)
    try:
        cb.click()
    except ElementClickInterceptedException:
        driver.execute_script("arguments[0].click();", cb)
    logger.info(f"  Checkbox '{label_substring}' -> {desired} gesetzt.")


def _check_required_checkbox(driver, css: str, description: str):
    """Setzt eine Pflicht-Checkbox (falls noch nicht gesetzt). Fehlt sie, Warnung."""
    boxes = driver.find_elements(By.CSS_SELECTOR, css)
    if not boxes:
        logger.warning(f"  Pflicht-Checkbox '{description}' nicht gefunden!")
        return
    cb = boxes[0]
    if cb.is_selected():
        logger.info(f"  Pflicht-Checkbox '{description}': bereits gesetzt.")
        return
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", cb)
    try:
        cb.click()
    except ElementClickInterceptedException:
        driver.execute_script("arguments[0].click();", cb)
    logger.info(f"  Pflicht-Checkbox '{description}' gesetzt.")


def join_as_moderator(driver, display_name: str, reuse_name: bool = True, mute_output: bool = False):
    """Namen eintragen, Checkboxen gezielt setzen, final betreten."""
    # 1) Anzeigename eintragen
    name_input = WebDriverWait(driver, WAITTIME).until(lambda d: _find_name_input(d))
    name_input.clear()
    name_input.send_keys(display_name[:40])  # maxlength=40
    logger.info(f"  Anzeigename gesetzt: {display_name}")

    # 2) Pflicht: Nutzungsbedingungen akzeptieren (sonst kein Beitritt moeglich)
    _check_required_checkbox(driver, CSS_TERMS_CHECKBOX, "Nutzungsbedingungen")

    # 3) Optionale Checkboxen gezielt nach Label setzen (nicht blind alle anhaken)
    _set_checkbox_by_label(driver, CB_REUSE_NAME_LABEL, reuse_name)
    _set_checkbox_by_label(driver, CB_MUTE_OUTPUT_LABEL, mute_output)

    # 4) Final betreten (stabiler Test-Hook, XPath-Fallback)
    try:
        wait_and_click(driver, By.CSS_SELECTOR, CSS_JOIN_SUBMIT,
                       "Raum als Moderator betreten", timeout=8)
    except TimeoutException:
        wait_and_click(driver, By.XPATH, XP_JOIN_SUBMIT,
                       "Raum als Moderator betreten (Fallback)")
    logger.info("Beitritt als Moderator*in ausgeloest.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    config = load_config()
    driver = create_webdriver(headless=config["headless"], fake_media=config["fake_media"])

    try:
        login(driver, config["username"], config["password"])

        # Sonderfall: direkter Raum-Link -> genau ein Raum, ohne Liste/Dialog.
        if config["room_url"]:
            driver.get(config["room_url"])
            wait_for_page_load(driver)
            logger.info(f"Direkter Raum geoeffnet: {config['room_url']}")
            join_as_moderator(driver, config["display_name"],
                              config["reuse_name"], config["mute_output"])
            logger.info("FERTIG: Raum als Moderator*in betreten.")
        else:
            # Alle Dauerraeume ermitteln (optional per VIKO_ROOM_NAME gefiltert).
            go_to_dauerraeume(driver)
            names = [n for n, _ in list_rooms(driver) if n]
            if config["room_name"]:
                flt = config["room_name"].lower()
                names = [n for n in names if flt in n.lower()]
            if not names:
                raise WebDriverException("Keine passenden Dauerraeume gefunden.")

            logger.info(f"Betrete {len(names)} Raum/Raeume nacheinander (je eigener Tab).")
            results = {}
            for idx, name in enumerate(names):
                try:
                    if idx > 0:
                        driver.switch_to.new_window("tab")  # eigener Tab pro Raum
                    join_single_room(driver, name, config["display_name"],
                                     config["reuse_name"], config["mute_output"])
                    results[name] = "OK"
                    logger.info(f"  -> '{name}' als Moderator*in betreten.")
                except Exception as e:
                    results[name] = f"FEHLER: {e}"
                    logger.error(f"  -> Raum '{name}' fehlgeschlagen: {e}")

            logger.info("=" * 50)
            logger.info("ZUSAMMENFASSUNG")
            logger.info("=" * 50)
            for name, status in results.items():
                logger.info(f"  {name}: {status}")

        if config["keep_open"] == "True":
            input("Alle Tabs offen. Zum Beenden Enter druecken...")

    except Exception as e:
        logger.error(f"Fehler: {e}")
        raise
    finally:
        if config["keep_open"] != "True":
            driver.quit()
            logger.info("Browser geschlossen")


if __name__ == "__main__":
    main()